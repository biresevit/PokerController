using System.Diagnostics;
using System.IO.Compression;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Runtime.InteropServices;

record Config(string BaseUrl,int MachineId,int SiteId,string Token,string[] TrustedKeys,string Root,string DecisionHealthUrl,string ExpectedMac="");
record Apps {
 public string? ReleaseId {get;set;}
 public string DecisionExe {get;set;}="";
 public string DecisionCwd {get;set;}="";
 public int DecisionPid {get;set;}
 public long DecisionStarted {get;set;}
 public string WorkerExe {get;set;}="";
 public string? WorkerProcessExe {get;set;}
 public string WorkerCwd {get;set;}="";
 public string[] WorkerArgs {get;set;}=[];
 public int WorkerPid {get;set;}
 public long WorkerStarted {get;set;}
 public string WorkerOutput {get;set;}="";
}
record Job(string Id,string Status,bool Paused,[property:System.Text.Json.Serialization.JsonPropertyName("release_id")] string ReleaseId,string Manifest,string Signature,string Url);
record Poll(Job? Task,[property:System.Text.Json.Serialization.JsonPropertyName("installed_release_id")] string? InstalledReleaseId=null);
record Manifest(int Format,string ReleaseId,string PdecisionVersion,string ControllerVersion,string Sha256,long Size);
record Operation(string TaskId,Apps Previous,Apps Target,string Phase);

static class Program {
 static readonly JsonSerializerOptions Json=new(JsonSerializerDefaults.Web){WriteIndented=true};
 static Config C=null!;
 static readonly HttpClient Api=new(){Timeout=TimeSpan.FromSeconds(25)};
 static string Instance=Guid.NewGuid().ToString();
 static string StateFile=>Path.Combine(C.Root,"active.json");
 static string OperationFile=>Path.Combine(C.Root,"operation.json");
 static string LogFile=>Path.Combine(C.Root,"updater.log");
 static readonly SemaphoreSlim StateLock=new(1,1);
 static void Log(string value){lock(Api)File.AppendAllText(LogFile,$"{DateTime.UtcNow:O} {value.Split('?')[0]}{Environment.NewLine}");}
 static T Read<T>(string path){if(path.EndsWith(".protected",StringComparison.OrdinalIgnoreCase))return JsonSerializer.Deserialize<T>(SecretStore.Read(path),Json)!;using var stream=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete);return JsonSerializer.Deserialize<T>(stream,Json)!;}
 static void Save<T>(string path,T value){File.WriteAllText(path+".tmp",JsonSerializer.Serialize(value,Json));File.Move(path+".tmp",path,true);}
 static string Under(string path,string root){var full=Path.GetFullPath(path);var parent=Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar)+Path.DirectorySeparatorChar;if(!full.StartsWith(parent,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("Path escapes the installation directory.");return full;}
 static Process? Owned(int pid,long started,string executable){
  try{var p=Process.GetProcessById(pid);if(!p.HasExited&&p.StartTime.ToUniversalTime().Ticks==started&&string.Equals(p.MainModule?.FileName,executable,StringComparison.OrdinalIgnoreCase))return p;}catch{}
  return null;
 }
 static async Task<T> Request<T>(string action,object body){
  using var req=new HttpRequestMessage(HttpMethod.Post,$"{C.BaseUrl.TrimEnd('/')}/api/updater/{C.MachineId}/{action}");
  req.Headers.Add("X-Updater-Token",C.Token);req.Content=new StringContent(JsonSerializer.Serialize(body,Json),Encoding.UTF8,"application/json");
  using var response=await Api.SendAsync(req);response.EnsureSuccessStatusCode();return (await response.Content.ReadFromJsonAsync<T>(Json))!;
 }
 static async Task<bool> RuntimeHealthy(){
  try{
   if(!File.Exists(StateFile))return false;var apps=Read<Apps>(StateFile);
   if(Owned(apps.DecisionPid,apps.DecisionStarted,apps.DecisionExe)==null||Owned(apps.WorkerPid,apps.WorkerStarted,apps.WorkerProcessExe??apps.WorkerExe)==null)return false;
   var status=Read<JsonElement>(Path.Combine(apps.WorkerOutput,"status.json"));
   if(!status.TryGetProperty("at",out var at)||!DateTimeOffset.TryParse(at.GetString(),out var stamp)||stamp<DateTimeOffset.UtcNow.AddSeconds(-45))return false;
   using var client=new HttpClient{Timeout=TimeSpan.FromSeconds(3)};
   using var response=await client.GetAsync(C.DecisionHealthUrl);if(!response.IsSuccessStatusCode)return false;
   var health=await response.Content.ReadFromJsonAsync<JsonElement>();return health.GetProperty("status").GetString()=="ready";
  }catch{return false;}
 }
 static async Task<Poll> Poll()=>await Request<Poll>("poll",new{instance_id=Instance,agent_version="1.0.13",apps_running=await RuntimeHealthy(),needs_install=!File.Exists(StateFile)&&!File.Exists(OperationFile)});
 static Task<JsonElement> Report(Job job,string phase,string detail="",Manifest? manifest=null)=>Request<JsonElement>("report",new{instance_id=Instance,agent_version="1.0.13",task_id=job.Id,phase,detail,pdecision_version=manifest?.PdecisionVersion,controller_version=manifest?.ControllerVersion});
 static Manifest Verify(Job job){
  byte[] bytes=Encoding.UTF8.GetBytes(job.Manifest),signature=Convert.FromBase64String(job.Signature);
  bool valid=false;foreach(var key in C.TrustedKeys){using var rsa=RSA.Create();rsa.ImportFromPem(key);if(rsa.VerifyData(bytes,signature,HashAlgorithmName.SHA256,RSASignaturePadding.Pss)){valid=true;break;}}
  if(!valid)throw new InvalidDataException("The release signature is not trusted.");
  var m=JsonSerializer.Deserialize<Manifest>(job.Manifest,Json)!;
  if(m.Format!=1||m.ReleaseId!=job.ReleaseId||m.Size<1||m.Size>2L*1024*1024*1024||m.Sha256.Length!=64)throw new InvalidDataException("Invalid release manifest.");
  return m;
 }
 static async Task<string> Stage(Job job,Manifest m){
  var version=Under(Path.Combine(C.Root,"versions",Guid.Parse(job.ReleaseId).ToString()),C.Root);
  var marker=Path.Combine(version,".package-sha256");
  if(File.Exists(marker)&&File.ReadAllText(marker)==m.Sha256)return version;
  var url=new Uri(job.Url);
  if(url.Scheme!="https"||!url.Host.EndsWith(".blob.core.windows.net",StringComparison.OrdinalIgnoreCase)||!string.IsNullOrEmpty(url.UserInfo))throw new InvalidDataException("Release download must come from Azure Blob Storage over HTTPS.");
  Directory.CreateDirectory(Path.Combine(C.Root,"downloads"));Directory.CreateDirectory(Path.Combine(C.Root,"versions"));
  var zip=Under(Path.Combine(C.Root,"downloads",job.ReleaseId+".zip"),C.Root);
  if(!File.Exists(zip)||new FileInfo(zip).Length!=m.Size||await Hash(zip)!=m.Sha256){
   using var handler=new HttpClientHandler{AllowAutoRedirect=false};using var client=new HttpClient(handler){Timeout=TimeSpan.FromMinutes(15)};
   using var response=await client.GetAsync(url,HttpCompletionOption.ResponseHeadersRead);response.EnsureSuccessStatusCode();
   await using var input=await response.Content.ReadAsStreamAsync();await using(var output=File.Create(zip+".partial")){
    byte[] buffer=new byte[131072];long total=0;int n;while((n=await input.ReadAsync(buffer))>0){total+=n;if(total>m.Size)throw new InvalidDataException("Release exceeds its signed size.");await output.WriteAsync(buffer.AsMemory(0,n));}
    if(total!=m.Size)throw new InvalidDataException("Incomplete release download.");
   }
   if(await Hash(zip+".partial")!=m.Sha256)throw new InvalidDataException("Release checksum did not match.");
   File.Move(zip+".partial",zip,true);
  }
  var stage=Under(Path.Combine(C.Root,"versions","stage-"+Guid.NewGuid()),C.Root);Directory.CreateDirectory(stage);
  using(var archive=ZipFile.OpenRead(zip)){
   if(archive.Entries.Count>20000||archive.Entries.Sum(e=>e.Length)>4L*1024*1024*1024)throw new InvalidDataException("Release archive is too large.");
   foreach(var entry in archive.Entries){
    if(entry.FullName.Contains(':'))throw new InvalidDataException("Invalid archive path.");
    var path=Under(Path.Combine(stage,entry.FullName.Replace('/',Path.DirectorySeparatorChar)),stage);
    var type=(entry.ExternalAttributes>>16)&0xf000;if(type==0xa000)throw new InvalidDataException("Release contains a symbolic link.");
    if(entry.FullName.EndsWith('/')){Directory.CreateDirectory(path);continue;}
    Directory.CreateDirectory(Path.GetDirectoryName(path)!);entry.ExtractToFile(path,false);
   }
  }
  if(!File.Exists(Path.Combine(stage,"pdecision","PDecision.exe"))||!File.Exists(Path.Combine(stage,"controller","PokerController.exe")))throw new InvalidDataException("The release is missing an application.");
  File.WriteAllText(Path.Combine(stage,".package-sha256"),m.Sha256);
  if(Directory.Exists(version))throw new InvalidDataException("An incomplete release directory already exists; manual review is required.");
  Directory.Move(stage,version);return version;
 }
 static async Task<string> Hash(string path){await using var stream=File.OpenRead(path);return Convert.ToHexString(await SHA256.HashDataAsync(stream)).ToLowerInvariant();}
 delegate bool EnumWindow(IntPtr window,IntPtr data);
 [DllImport("user32.dll")]static extern bool EnumWindows(EnumWindow callback,IntPtr data);
 [DllImport("user32.dll")]static extern uint GetWindowThreadProcessId(IntPtr window,out uint process);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]static extern int GetWindowTextLength(IntPtr window);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)]static extern int GetClassName(IntPtr window,StringBuilder name,int length);
 [DllImport("user32.dll")]static extern bool PostMessage(IntPtr window,uint message,IntPtr wparam,IntPtr lparam);
 static bool CloseDecision(Process process){
  if(process.CloseMainWindow())return true;
  bool posted=false;
  EnumWindows((window,_)=>{GetWindowThreadProcessId(window,out var pid);if(pid!=process.Id||GetWindowTextLength(window)==0)return true;
   var name=new StringBuilder(256);GetClassName(window,name,256);
   if(name.ToString().StartsWith("WindowsForms10.",StringComparison.Ordinal))posted|=PostMessage(window,0x0010,IntPtr.Zero,IntPtr.Zero);
   return true;
  },IntPtr.Zero);
  return posted;
 }
 static async Task StopWorker(Apps apps){
  var worker=Owned(apps.WorkerPid,apps.WorkerStarted,apps.WorkerProcessExe??apps.WorkerExe);
  if(worker!=null){
   if(string.IsNullOrEmpty(apps.WorkerOutput)||!Directory.Exists(apps.WorkerOutput))throw new InvalidDataException("Worker output directory is not configured.");
   File.WriteAllText(Path.Combine(apps.WorkerOutput,"stop"),"Software update; all sessions have drained.");
   using var timeout=new CancellationTokenSource(TimeSpan.FromMinutes(3));await worker.WaitForExitAsync(timeout.Token);
  }
 }
 static async Task Stop(Apps apps){
  await StopWorker(apps);
  var decision=Owned(apps.DecisionPid,apps.DecisionStarted,apps.DecisionExe);
  if(decision!=null){
   if(!CloseDecision(decision))throw new InvalidOperationException("PDecision did not accept a graceful shutdown.");
   using var timeout=new CancellationTokenSource(TimeSpan.FromMinutes(2));await decision.WaitForExitAsync(timeout.Token);
  }
 }
 // Immediate update: the operator explicitly chose to stop now regardless of open hands/sessions, so this
 // skips the cooperative "stop" sentinel and graceful window-close, and kills both process trees outright.
 static async Task KillNow(Apps apps){
  var worker=Owned(apps.WorkerPid,apps.WorkerStarted,apps.WorkerProcessExe??apps.WorkerExe);
  if(worker!=null){
   try{worker.Kill(true);}catch{}
   using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(30));try{await worker.WaitForExitAsync(timeout.Token);}catch{}
  }
  var decision=Owned(apps.DecisionPid,apps.DecisionStarted,apps.DecisionExe);
  if(decision!=null){
   try{decision.Kill(true);}catch{}
   using var timeout=new CancellationTokenSource(TimeSpan.FromSeconds(30));try{await decision.WaitForExitAsync(timeout.Token);}catch{}
  }
 }
 static void AddSettings(ProcessStartInfo info,JsonElement value,string prefix){
  if(value.ValueKind==JsonValueKind.Object){foreach(var prop in value.EnumerateObject())AddSettings(info,prop.Value,prefix+prop.Name+"__");}
  else if(value.ValueKind==JsonValueKind.Array){int i=0;foreach(var item in value.EnumerateArray())AddSettings(info,item,prefix+(i++)+"__");}
  else if(value.ValueKind!=JsonValueKind.Null)info.Environment[prefix.TrimEnd('_')]=value.ValueKind==JsonValueKind.String?value.GetString()!:value.ToString();
 }
 static Process Launch(string exe,string cwd,IEnumerable<string> args){
  var info=new ProcessStartInfo(exe){WorkingDirectory=cwd,UseShellExecute=false,CreateNoWindow=true};foreach(var arg in args)info.ArgumentList.Add(arg);
  var protectedEnvironment=Path.Combine(C.Root,"config","worker-env.protected");
  var environment=File.Exists(protectedEnvironment)?protectedEnvironment:Path.Combine(C.Root,"config","worker-env.json");
  if(Path.GetFileName(exe).Equals("PDecision.exe",StringComparison.OrdinalIgnoreCase)){
   var protectedSettings=Path.Combine(C.Root,"config","pdecision.protected");
   if(File.Exists(protectedSettings))AddSettings(info,Read<JsonElement>(protectedSettings),"PDECISION_");
  }
  if(File.Exists(environment))foreach(var pair in Read<Dictionary<string,string>>(environment)){
   if(pair.Key is not ("SCHEDULE_SQL_CONNECTION_STRING" or "SqlConnectionString" or "SCHEDULE_ODBC_DRIVER"))throw new InvalidDataException("Unsupported runtime setting.");
   info.Environment[pair.Key]=pair.Value;
  }
  return Process.Start(info)??throw new InvalidOperationException("Application did not start.");
 }
 static void EnsureRunning(Process process,string name){if(process.HasExited)throw new InvalidOperationException($"{name} exited during startup (exit code {process.ExitCode}).");}
 static async Task<string> DecisionHealth(Process decision,string? expected=null){
  using var client=new HttpClient{Timeout=TimeSpan.FromSeconds(3)};var deadline=DateTime.UtcNow.AddHours(2);int stable=0;string version="";
  Log("Waiting up to two hours for PDecision readiness; initial resource sync may take an hour. PokerController starts afterward.");
  while(DateTime.UtcNow<deadline){
   EnsureRunning(decision,"PDecision");
   try{using var r=await client.GetAsync(C.DecisionHealthUrl);if(r.IsSuccessStatusCode){var json=await r.Content.ReadFromJsonAsync<JsonElement>();version=json.GetProperty("version").GetString()!;
     if(json.GetProperty("status").GetString()=="ready"&&(expected==null||version==expected)){if(++stable>=3)return version;}else stable=0;}else stable=0;}catch{stable=0;}
   await Task.Delay(2000);
  }
  throw new TimeoutException("PDecision did not become healthy within two hours; check resource synchronization and the PDecision health response.");
 }
 static async Task Start(Apps apps,Manifest? expected=null){
  Directory.CreateDirectory(Path.Combine(C.Root,"runs"));
  var decision=Owned(apps.DecisionPid,apps.DecisionStarted,apps.DecisionExe);
  if(decision==null){decision=Launch(apps.DecisionExe,apps.DecisionCwd,[]);apps.DecisionPid=decision.Id;apps.DecisionStarted=decision.StartTime.ToUniversalTime().Ticks;Save(StateFile,apps);}
  await DecisionHealth(decision,expected?.PdecisionVersion);
  Log("PDecision is ready. Starting PokerController.");
  var worker=Owned(apps.WorkerPid,apps.WorkerStarted,apps.WorkerProcessExe??apps.WorkerExe);
  if(worker==null){
   var output=Under(Path.Combine(C.Root,"runs",DateTime.UtcNow.ToString("yyyyMMdd-HHmmss")+"-"+Guid.NewGuid().ToString("N")[..6]),C.Root);Directory.CreateDirectory(output);
   var args=apps.WorkerArgs.ToList();var index=args.IndexOf("--output");if(index>=0&&index+1<args.Count){args[index+1]=output;}else{args.Add("--output");args.Add(output);}
   apps.WorkerOutput=output;apps.WorkerArgs=args.ToArray();worker=Launch(apps.WorkerExe,apps.WorkerCwd,args);apps.WorkerProcessExe=apps.WorkerExe;apps.WorkerPid=worker.Id;apps.WorkerStarted=worker.StartTime.ToUniversalTime().Ticks;Save(StateFile,apps);
  }
  var deadline=DateTime.UtcNow.AddMinutes(5);var status=Path.Combine(apps.WorkerOutput,"status.json");
  while(DateTime.UtcNow<deadline){
   EnsureRunning(worker,"PokerController");
   if(File.Exists(status)){
    try{var json=JsonDocument.Parse(File.ReadAllText(status)).RootElement;if(expected==null||(json.TryGetProperty("controller_version",out var v)&&v.GetString()==expected.ControllerVersion))return;}catch{}
   }
   await Task.Delay(2000);
  }
  throw new TimeoutException("PokerController did not publish a healthy startup status.");
 }
 static Apps Target(string version,Job job)=>new(){ReleaseId=job.ReleaseId,DecisionExe=Path.Combine(version,"pdecision","PDecision.exe"),DecisionCwd=Path.Combine(version,"pdecision"),WorkerExe=Path.Combine(version,"controller","PokerController.exe"),WorkerCwd=Path.Combine(version,"controller"),WorkerArgs=["--site-id",C.SiteId.ToString(),"--machine-id",C.MachineId.ToString()]};
 static async Task Install(Job job,Manifest manifest,string version){
  var previous=File.Exists(StateFile)?Read<Apps>(StateFile):new Apps();var target=Target(version,job);
  if(File.Exists(OperationFile)){
   var saved=Read<Operation>(OperationFile);if(saved.TaskId!=job.Id)throw new InvalidOperationException("A different update needs recovery.");previous=saved.Previous;target=saved.Target;var active=Read<Apps>(StateFile);if(active.ReleaseId==job.ReleaseId)target=active;
  }
  if(job.Status!="verifying")await Report(job,"installing");Save(OperationFile,new Operation(job.Id,previous,target,"installing"));
  try{
   if(job.Status!="verifying")await Stop(previous);
   var settings=Path.Combine(C.Root,"config","pdecision.json");
   if(File.Exists(Path.Combine(C.Root,"config","pdecision.protected")))File.WriteAllText(Path.Combine(target.DecisionCwd,"appsettings.json"),"{}");
   else {if(!File.Exists(settings))throw new InvalidOperationException("Machine-specific PDecision settings are missing.");File.Copy(settings,Path.Combine(target.DecisionCwd,"appsettings.json"),true);}
   Save(StateFile,target);await Report(job,"verifying");Save(OperationFile,new Operation(job.Id,previous,target,"verifying"));
   await Start(target,manifest);
   try{await Report(job,"resuming",manifest:manifest);}
   catch(Exception error) when(error is HttpRequestException or TaskCanceledException){
    Save(OperationFile,new Operation(job.Id,previous,target,"awaiting_resume"));
    Log("Applications are healthy; waiting for control-plane acknowledgement. "+error.Message);return;
   }
   File.Delete(OperationFile);Log("Update healthy; traffic recovery is now controlled by the scheduler.");
  }catch(Exception error){
   Log("Update failed: "+error.Message);
   try{
    if(string.IsNullOrEmpty(previous.DecisionExe)){
     await Stop(target);if(File.Exists(StateFile))File.Delete(StateFile);
     await Report(job,"failed","Initial installation failed; no previous release exists. "+error.Message.Split('?')[0]);
     File.Delete(OperationFile);return;
    }
    // Never install a second release over an unverified first attempt.
    await Stop(target);Save(StateFile,previous);await Start(previous);
    await Report(job,"rolled_back","The update failed; the previous applications are healthy again. "+error.Message.Split('?')[0]);
    File.Delete(OperationFile);Log("Rolled back to the previous application pair.");
   }catch(Exception rollback){
    Save(OperationFile,new Operation(job.Id,previous,target,"failed"));
    await Report(job,"failed","Automatic recovery could not be verified. Manual review is required. "+rollback.Message.Split('?')[0]);
    Log("Recovery failed: "+rollback.Message);
   }
  }
 }
 static void SelfTest(){
  using var rsa=RSA.Create(3072);var id=Guid.NewGuid().ToString();
  C=new Config("https://example.invalid",1,1,"test",[rsa.ExportSubjectPublicKeyInfoPem()],Path.Combine(Path.GetTempPath(),"PokerSuite-updater-selftest"),"http://127.0.0.1:5005/api/health");
  var manifest=JsonSerializer.Serialize(new{format=1,releaseId=id,pdecisionVersion="1.0.0.29",controllerVersion="0.1.0",sha256=new string('0',64),size=1});
  var signature=Convert.ToBase64String(rsa.SignData(Encoding.UTF8.GetBytes(manifest),HashAlgorithmName.SHA256,RSASignaturePadding.Pss));
  var job=new Job(id,"preparing",false,id,manifest,signature,"https://example.blob.core.windows.net/releases/test.zip");
  var roundtrip=JsonSerializer.Deserialize<Poll>(JsonSerializer.Serialize(new{task=new{id,status="preparing",paused=false,release_id=id,manifest,signature,url=job.Url}}),Json)!;
  if(roundtrip.Task?.ReleaseId!=id||Verify(roundtrip.Task).ReleaseId!=id)throw new Exception("API contract self-test failed");
  if(Verify(job).ReleaseId!=id)throw new Exception("Signature self-test failed");
  bool rejected=false;try{Verify(job with {Manifest=manifest.Replace("1.0.0.29","9.0.0.29")});}catch(InvalidDataException){rejected=true;}
  if(!rejected)throw new Exception("Tampered release was accepted");
  rejected=false;try{Under(Path.Combine(C.Root,"..","outside.exe"),C.Root);}catch(InvalidDataException){rejected=true;}
  if(!rejected)throw new Exception("Path traversal was accepted");
  foreach(var phase in new[]{"preparing","draining","resuming"})
   foreach(var paused in new[]{false,true})
    if(!ShouldSupervise(job with {Status=phase,Paused=paused}))throw new Exception("Existing applications must stay supervised during "+phase);
  foreach(var phase in new[]{"reconciling","stopping","installing","verifying"})
   if(ShouldSupervise(job with {Status=phase}))throw new Exception("Intentional application stop must not be undone during "+phase);
  if(!ShouldSupervise(null))throw new Exception("Ordinary process supervision must be enabled");
  var marker=Path.Combine(Path.GetTempPath(),Guid.NewGuid()+".startup-ready");
  var probeInfo=new ProcessStartInfo(Environment.ProcessPath!){UseShellExecute=false,CreateNoWindow=true};probeInfo.ArgumentList.Add("--startup-probe");probeInfo.ArgumentList.Add(marker);
  using(var probe=Process.Start(probeInfo)!){
   var until=DateTime.UtcNow.AddSeconds(5);
   while(!File.Exists(marker)&&DateTime.UtcNow<until){EnsureRunning(probe,"Startup probe");Thread.Sleep(10);}
   if(!File.Exists(marker))throw new Exception("Fresh child did not reach ready");
   if(!probe.WaitForExit(5000))throw new Exception("Probe did not stop");
   bool noticed=false;try{EnsureRunning(probe,"Startup probe");}catch(InvalidOperationException){noticed=true;}
   if(!noticed)throw new Exception("Exited child was treated as running");
  }
  File.Delete(marker);
 }
 static bool ShouldSupervise(Job? job)=>job==null||job.Status is "preparing" or "draining" or "resuming";
 static async Task Supervise(){
  if(!File.Exists(StateFile)||File.Exists(OperationFile))return;
  var active=Read<Apps>(StateFile);
  if(active.ReleaseId!=null&&(Owned(active.DecisionPid,active.DecisionStarted,active.DecisionExe)==null||Owned(active.WorkerPid,active.WorkerStarted,active.WorkerProcessExe??active.WorkerExe)==null)){
   Log("Restarting a stopped managed application.");await Start(active);
  }
 }
 static async Task Main(string[] args){
  if(args.Length==2&&args[0]=="--startup-probe"){await Task.Delay(250);File.WriteAllText(args[1],"ready");return;}
  if(args.Contains("--self-test")){SelfTest();return;}
  var configIndex=Array.IndexOf(args,"--config");if(configIndex<0||configIndex+1>=args.Length)return;
  C=Read<Config>(args[configIndex+1]);Directory.CreateDirectory(C.Root);
  if(!PokerSuite.Shared.Utilities.MachineNetworkIdentity.Matches(C.ExpectedMac,PokerSuite.Shared.Utilities.MachineNetworkIdentity.GetMacAddresses())){Log("Enrollment does not match a network adapter on this PC.");return;}
  if(new Uri(C.BaseUrl).Scheme!="https"&&!new Uri(C.BaseUrl).IsLoopback)throw new InvalidDataException("Updater API must use HTTPS.");
  using var mutex=new Mutex(true,$"Local\\PokerSuiteUpdater-{C.MachineId}",out var owner);if(!owner)return;
  Log("Updater started.");
  // Heartbeats remain independent of downloads, graceful stops and startup.
  _=Task.Run(async()=>{while(true){try{await Poll();}catch{}await Task.Delay(10000);}});
  var nextStart=DateTime.MinValue;
  while(true){
   Job? job=null;
   try{
    // The last server-approved mode survives a temporary network outage.
    var supervision=Path.Combine(C.Root,"supervision.json");
    if(File.Exists(supervision)&&Read<bool>(supervision)&&DateTime.UtcNow>=nextStart){nextStart=DateTime.UtcNow.AddSeconds(90);await Supervise();}
    var reply=await Poll();job=reply.Task;
    Save(supervision,ShouldSupervise(job));
    if(File.Exists(OperationFile)){
     var pending=Read<Operation>(OperationFile);
     if(pending.Phase=="awaiting_resume"&&((job?.Status=="resuming"&&job.ReleaseId==pending.Target.ReleaseId)||(job==null&&reply.InstalledReleaseId==pending.Target.ReleaseId))){
      File.Delete(OperationFile);Log("Healthy application switch acknowledged by the control plane.");
     }
    }
    if(ShouldSupervise(job)&&DateTime.UtcNow>=nextStart){nextStart=DateTime.UtcNow.AddSeconds(90);await Supervise();}
    if(job!=null){
     var manifest=Verify(job);
     if(job.Status is "preparing" or "draining"){
      await Stage(job,manifest);await Report(job,"staged");
     }else if(!job.Paused&&job.Status=="forcing"){
      await KillNow(Read<Apps>(StateFile));await Report(job,"force_stopped");
     }else if(!job.Paused&&job.Status=="reconciling"){
      await Report(job,"reconciling");await StopWorker(Read<Apps>(StateFile));await Report(job,"reconciled");
     }else if(!job.Paused&&(job.Status is "stopping" or "installing" or "verifying")){
      if(File.Exists(OperationFile)&&Read<Operation>(OperationFile).Phase=="failed"){Log("Waiting for manual recovery of the interrupted update.");}
      else {var version=await Stage(job,manifest);await Install(job,manifest,version);}
     }
    }
   }catch(Exception error){
    Log("Updater check failed: "+error.Message);
    if(job!=null&&(job.Status is "preparing" or "draining")&&(error is InvalidDataException or CryptographicException)){
     try{await Report(job,"failed","The release could not be verified. Existing applications were not stopped. "+error.Message.Split('?')[0]);}catch{}
    }
   }
   await Task.Delay(5000);
  }
 }
}
