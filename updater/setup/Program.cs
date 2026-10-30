using System.Diagnostics;
using System.Net.Http.Json;
using System.Net.NetworkInformation;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.IO.Compression;

static class Program {
 [STAThread]static void Main(string[] args){
  if(args.Contains("--prerequisite-self-test")){PrerequisiteInstaller.SelfTest().GetAwaiter().GetResult();return;}
  if(args.Contains("--enrollment-self-test")){InstalledEnrollment.SelfTest();return;}
  if(args.Contains("--status-ui-self-test")){ApplicationConfiguration.Initialize();SetupForm.InterfaceSelfTest().GetAwaiter().GetResult();return;}
  if(args.Contains("--self-test")){var p=Path.Combine(Path.GetTempPath(),Guid.NewGuid()+".protected");SecretStore.Write(p,"test-secret");if(SecretStore.Read(p)!="test-secret"||Encoding.UTF8.GetString(File.ReadAllBytes(p)).Contains("test-secret"))throw new Exception("DPAPI failure");File.Delete(p);return;}
  ApplicationConfiguration.Initialize();Application.Run(new SetupForm());
 }
}
class SetupForm:Form {
 const string Base="https://lively-rock-032158003.4.azurestaticapps.net";
 readonly HttpClient client=new(){Timeout=TimeSpan.FromSeconds(25)};
 readonly string enrollmentFolder;readonly bool testMode;
 readonly TextBox pairing=new(){ReadOnly=true,Font=new Font("Segoe UI",24,FontStyle.Bold),Width=400,Visible=false};
 readonly Button start=new(){Text="Get pairing code",AutoSize=true};
 readonly Button installPrerequisites=new(){Text="Install missing components",AutoSize=true};
 readonly Button restoreInstallation=new(){Text="Restore approved installation",AutoSize=true,Visible=false};
 readonly Label versionDetails=new(){AutoSize=true,MaximumSize=new Size(660,0),Font=new Font("Segoe UI",10,FontStyle.Bold)};
 bool canPair=true;
 readonly ToolStripStatusLabel footer=new(){Spring=true,TextAlign=ContentAlignment.MiddleLeft};
 readonly Label enrollmentDetails=new(){AutoSize=true,MaximumSize=new Size(660,0)};
 readonly Label runtimeDetails=new(){AutoSize=true,MaximumSize=new Size(660,0)};
 readonly Label taskDetails=new(){AutoSize=true,MaximumSize=new Size(660,0)};
 readonly Label intro=new(){AutoSize=true,MaximumSize=new Size(660,0)};
 DateTime nextAgentStart=DateTime.MinValue;
 readonly Label status=new(){AutoSize=true,MaximumSize=new Size(660,0)};
 readonly string local=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"PokerSuite","Setup");
 RSA key=RSA.Create(3072);string? requestId;bool busy;readonly System.Windows.Forms.Timer timer=new(){Interval=5000};
 public SetupForm(string? agentFolder=null,string? identityFolder=null,HttpMessageHandler? statusHandler=null){
  enrollmentFolder=agentFolder??@"C:\PokerSuite\Updater";testMode=statusHandler!=null;if(identityFolder!=null)local=identityFolder;if(statusHandler!=null)client=new HttpClient(statusHandler);
  var setupVersion=Assembly.GetExecutingAssembly().GetName().Version!.ToString(3);
  Text="PokerSuite Setup and Status "+setupVersion;ClientSize=new Size(820,680);MinimumSize=new Size(760,600);Font=new Font("Segoe UI",10);StartPosition=FormStartPosition.CenterScreen;
  var layout=new FlowLayoutPanel{Dock=DockStyle.Fill,FlowDirection=FlowDirection.TopDown,WrapContents=false,Padding=new Padding(24),AutoScroll=true};Controls.Add(layout);
  layout.Controls.Add(new Label{Text="Install PokerSuite on this PC",Font=new Font(Font,FontStyle.Bold),AutoSize=true});
  versionDetails.Text=$"Setup version: {setupVersion} | Bundled updater agent: 1.0.12\nInstalled updater agent: {InstalledAgentVersion()}";layout.Controls.Add(versionDetails);
  var macs=Addresses();intro.Text=$"PC: {Environment.MachineName}\nNetwork adapters: {string.Join(", ",macs)}\n\nGet a pairing code and give it to your main administrator. Approve it in Backoffice > Updates. Keep this window open. No JSON files are required.";layout.Controls.Add(intro);
  layout.Controls.Add(pairing);layout.Controls.Add(installPrerequisites);layout.Controls.Add(restoreInstallation);layout.Controls.Add(start);layout.Controls.Add(status);
  layout.Controls.Add(enrollmentDetails);layout.Controls.Add(runtimeDetails);layout.Controls.Add(taskDetails);
  var bar=new StatusStrip();bar.Items.Add(footer);Controls.Add(bar);bar.BringToFront();
  status.TextChanged+=(_,_)=>{footer.Text=status.Text.Replace("\n"," ");footer.ToolTipText=status.Text;};
  status.Text="Checking saved authentication and installation...";
  enrollmentDetails.Text="Authentication: checking this Windows account...";
  runtimeDetails.Text="Updater: checking local files. PDecision/PokerController: status not yet available.";
  taskDetails.Text="Current action: checking existing enrollment before offering pairing.";
  var help=new LinkLabel{Text="Windows prerequisites and setup help",AutoSize=true};help.LinkClicked+=(_,_)=>Process.Start(new ProcessStartInfo(Base+"/pc-pairing-installation.pdf"){UseShellExecute=true});layout.Controls.Add(help);
  start.Click+=async(_,_)=>await Begin();timer.Tick+=async(_,_)=>await Check();FormClosed+=(_,_)=>timer.Stop();
  Shown+=async(_,_)=>await InitializeState();
  installPrerequisites.Click+=async(_,_)=>await InstallPrerequisites();
  restoreInstallation.Click+=async(_,_)=>await RestoreInstallation();
  if(macs.Length==0){start.Enabled=false;status.Text="Connect a physical Ethernet or Wi-Fi adapter first.";}
  if(!testMode&&File.Exists(@"C:\PokerSuite\Managed\active.json")){start.Enabled=false;status.Text="This PC already has a PokerSuite installation. Use Backoffice Updates; setup will not overwrite it.";}
  RecognizeEnrollment();
  canPair=start.Enabled;
  var missing=Prerequisites();installPrerequisites.Visible=missing.Count>0;if(missing.Count>0){start.Enabled=false;status.Text="Required Windows components: "+string.Join(", ",missing)+". Click Install missing components; their Microsoft installers are included.";}
 }
 string? EnrollmentPath()=>InstalledEnrollment.Find(enrollmentFolder);
 internal static async Task InterfaceSelfTest(){
  var folder=Path.Combine(Path.GetTempPath(),"PokerSuite-status-ui-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(folder);
  var singleFile=Path.Combine(folder,"single-file-agent");Directory.CreateDirectory(singleFile);
  using(var payload=Assembly.GetExecutingAssembly().GetManifestResourceStream("PokerSuite.Setup.agent.zip")!)
  using(var zip=new ZipArchive(payload))zip.GetEntry("PokerSuite.Updater.exe")!.ExtractToFile(Path.Combine(singleFile,"PokerSuite.Updater.exe"));
  if(!InstalledAgentVersion(singleFile).StartsWith("1.0.12")||File.Exists(Path.Combine(singleFile,"PokerSuite.Updater.dll")))throw new Exception("Single-file updater was not detected without a DLL");
  File.WriteAllText(Path.Combine(singleFile,"PokerSuite.Updater.dll"),"stale legacy sidecar");
  if(!InstalledAgentVersion(singleFile).StartsWith("1.0.12"))throw new Exception("Stale DLL hid the installed executable version");
  var agent=Path.Combine(folder,"agent");var identity=Path.Combine(folder,"identity");Directory.CreateDirectory(agent);Directory.CreateDirectory(identity);
  using(var device=RSA.Create(3072))SecretStore.Write(Path.Combine(identity,"device-key.protected"),device.ExportPkcs8PrivateKeyPem());
  File.WriteAllText(Path.Combine(identity,"request.json"),JsonSerializer.Serialize(new{id=Guid.NewGuid().ToString(),code="TESTDEVICE",expires=DateTimeOffset.UtcNow.AddDays(-2)}));
  using(var missing=new SetupForm(agent,identity,new StatusFixture())){
   await missing.InitializeState();
   if(missing.start.Enabled||!missing.status.Text.Contains("Approved: machine 10")||!missing.taskDetails.Text.Contains("Restore approved installation")||!missing.versionDetails.Text.Contains("Setup version:"))throw new Exception("Approved device with missing enrollment did not show recovery status");
  }
  File.WriteAllText(Path.Combine(agent,"agent.json"),JsonSerializer.Serialize(new InstalledEnrollment(10,1,new string('x',48),"https://example.invalid",folder)));
  using(var installed=new SetupForm(agent,identity,new StatusFixture())){
   await installed.InitializeState();
   if(installed.start.Enabled||!installed.status.Text.Contains("Authenticated: machine 10")||!installed.taskDetails.Text.Contains("failed")||!installed.versionDetails.Text.Contains("Last connected agent: 1.0.8"))throw new Exception("Existing enrollment did not show version and task status");
  }
  var empty=Path.Combine(folder,"empty");Directory.CreateDirectory(empty);
  using(var fresh=new SetupForm(empty,empty,new StatusFixture())){
   await fresh.InitializeState();if(!fresh.status.Text.Contains("Not authenticated locally")||!fresh.taskDetails.Text.Contains("No PDecision/PokerController download"))throw new Exception("Fresh PC had blank or misleading status");
  }
 }
 sealed class StatusFixture:HttpMessageHandler {
  protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request,CancellationToken cancellationToken){
   var snapshot=new{authenticated=true,machine_id=10,site_id=1,agent_online=false,agent_version="1.0.8",agent_heartbeat=(string?)null,apps_healthy=false,installed_release=(string?)null,pdecision_version=(string?)null,controller_version=(string?)null,update=new{status="failed",rollout_status="paused",release_label="test",detail="PDecision exited during startup."}};
   object reply=request.RequestUri!.AbsolutePath.EndsWith("/poll")?new{status="completed",device_status=snapshot}:snapshot;
   return Task.FromResult(new HttpResponseMessage(System.Net.HttpStatusCode.OK){Content=new StringContent(JsonSerializer.Serialize(reply),Encoding.UTF8,"application/json")});
  }
 }
 static string InstalledAgentVersion(string folder=@"C:\PokerSuite\Updater"){
  var path=Path.Combine(folder,"PokerSuite.Updater.exe");
  if(!File.Exists(path))return "Not installed (agent executable missing)";
  // Current releases embed their DLL and runtime in this single executable.
  // Prefer executable metadata even when old sidecar DLLs remain on disk.
  return FileVersionInfo.GetVersionInfo(path).FileVersion??"Unknown";
 }
 async Task InitializeState(){
  if(RecognizeEnrollment()){timer.Start();await Check();return;}
  var request=Path.Combine(local,"request.json");var savedKey=Path.Combine(local,"device-key.protected");
  if(File.Exists(request)&&File.Exists(savedKey)){
   start.Visible=false;pairing.Visible=false;start.Enabled=false;
   try{
    key.ImportFromPem(SecretStore.Read(savedKey));var saved=JsonDocument.Parse(File.ReadAllText(request)).RootElement;
    requestId=saved.GetProperty("id").GetString();pairing.Text=saved.GetProperty("code").GetString();
    status.Text="Local enrollment files are missing. Checking the saved device approval...";timer.Start();await Check();
   }catch(Exception){status.Text="Saved device identity cannot be opened. Sign in with the original Windows user or contact the administrator. No new pairing has been requested.";}
   return;
  }
  enrollmentDetails.Text="Authentication: no saved enrollment or pairing identity found for Windows user "+Environment.UserName+".";
  var runtimeMissing=Prerequisites();runtimeDetails.Text=(runtimeMissing.Count>0?"Runtime components missing: "+string.Join(", ",runtimeMissing):"Windows runtimes: ready")+"\nInstalled updater agent: "+InstalledAgentVersion()+".";
  taskDetails.Text="Current action: idle. No PDecision/PokerController download has been requested by this setup.";
  status.Text="Not authenticated locally | No download in progress | Request administrator pairing if this is a new installation.";
 }
 async Task RestoreInstallation(){
  restoreInstallation.Enabled=false;timer.Stop();
  try{
   if(Prerequisites().Count>0){status.Text="Install missing components before restoring the approved installation.";return;}
   if(File.Exists(@"C:\PokerSuite\Managed\operation.json")){status.Text="An unfinished local application update needs administrator review before restoring credentials.";return;}
   if(Process.GetProcessesByName("PokerSuite.Updater").Any()){status.Text="An updater process is already running. Wait for its connection to recover or ask the administrator to review it before restoring credentials.";return;}
   status.Text="Restoring this approved PC using its saved device key...";
   var reply=await Post("restore",Proof("restore"));await Install(Decrypt(reply.GetProperty("envelope")),restore:true);
   await Post("complete",Proof("complete"));restoreInstallation.Visible=false;
   RecognizeEnrollment();await MonitorEnrollment();
  }catch(Exception error){status.Text="Recovery did not complete: "+error.Message;}
  finally{restoreInstallation.Enabled=true;timer.Start();}
 }
 bool RecognizeEnrollment(){
  if(EnrollmentPath()==null)return false;
  canPair=false;start.Enabled=false;start.Visible=false;pairing.Visible=false;
  restoreInstallation.Visible=false;
  intro.Text=$"PC: {Environment.MachineName}\nThis PC already has an enrollment. Authentication and installation status refresh automatically below. No new pairing code is needed.";
  return true;
 }
 async Task MonitorEnrollment(){
  RecognizeEnrollment();
  InstalledEnrollment cfg;
  var path=EnrollmentPath()!;
  try{cfg=InstalledEnrollment.Load(path);}
  catch(Exception){status.Text="Enrollment found but cannot be opened by this Windows user. Sign in with the user who installed PokerSuite. Do not pair again.";return;}
  enrollmentDetails.Text=$"Machine {cfg.MachineId} | Site {cfg.SiteId} | Existing enrollment found. No pairing is needed.";
  var missing=Prerequisites();installPrerequisites.Visible=missing.Count>0;
  runtimeDetails.Text=missing.Count>0?"Runtime repair required: "+string.Join(", ",missing):"Windows runtimes: ready";
  if(!testMode&&missing.Count==0&&DateTime.UtcNow>=nextAgentStart&&!Process.GetProcessesByName("PokerSuite.Updater").Any()){
   nextAgentStart=DateTime.UtcNow.AddSeconds(90);
   var exe=Path.Combine(Path.GetDirectoryName(path)!,"PokerSuite.Updater.exe");
   if(File.Exists(exe)){
    var launch=new ProcessStartInfo(exe){WorkingDirectory=Path.GetDirectoryName(exe),UseShellExecute=false,CreateNoWindow=true};launch.ArgumentList.Add("--config");launch.ArgumentList.Add(path);Process.Start(launch);
   }
  }
  using var message=new HttpRequestMessage(HttpMethod.Post,$"{cfg.BaseUrl.TrimEnd('/')}/api/updater/{cfg.MachineId}/status");message.Headers.Add("X-Updater-Token",cfg.Token);message.Content=new StringContent("{}",Encoding.UTF8,"application/json");
  using var response=await client.SendAsync(message);
  if(response.StatusCode==System.Net.HttpStatusCode.Unauthorized){status.Text="Enrollment found, but Backoffice rejected its credentials. Administrator recovery is required; do not pair again.";return;}
  response.EnsureSuccessStatusCode();var value=await response.Content.ReadFromJsonAsync<JsonElement>();
  RenderStatus(value,true);
 }
 void RenderStatus(JsonElement value,bool localEnrollment){
  status.Text=InstalledEnrollment.Describe(value);
  string Text(string field){
   if(!value.TryGetProperty(field,out var item)||item.ValueKind==JsonValueKind.Null)return "Not reported";
   return field.EndsWith("heartbeat")&&DateTimeOffset.TryParse(item.ToString(),out var stamp)?stamp.ToLocalTime().ToString("g"):item.ToString();
  }
  var machine=value.GetProperty("machine_id").GetInt32();var site=value.GetProperty("site_id").GetInt32();
  versionDetails.Text=$"Setup version: {Assembly.GetExecutingAssembly().GetName().Version!.ToString(3)} | Bundled updater agent: 1.0.12\nInstalled updater agent: {InstalledAgentVersion()} | Last connected agent: {Text("agent_version")}";
  enrollmentDetails.Text=$"Authentication: approved | Machine {machine} | Site {site}\nUpdater heartbeat: {Text("agent_heartbeat")}";
  var missing=Prerequisites();installPrerequisites.Visible=missing.Count>0;
  runtimeDetails.Text=missing.Count>0?"Runtime repair required: "+string.Join(", ",missing):"Windows runtimes: ready";
  runtimeDetails.Text+=("\nInstalled release: "+Text("installed_release")+"\nPDecision: "+Text("pdecision_version")+" | PokerController: "+Text("controller_version")+"\nApplications: "+(value.GetProperty("apps_healthy").GetBoolean()?"healthy":"not yet healthy"));
  var update=value.GetProperty("update");
  taskDetails.Text=update.ValueKind==JsonValueKind.Object?$"Installation/update: {update.GetProperty("status").GetString()} | Rollout: {update.GetProperty("rollout_status").GetString()}\nRelease: {update.GetProperty("release_label").GetString()}\n{update.GetProperty("detail").GetString()}":"No installation/update task. Schedule a release in Backoffice Updates.";
  if(update.ValueKind==JsonValueKind.Object&&update.GetProperty("status").GetString() is "failed" or "rolled_back")taskDetails.Text+="\nAfter correcting the reported problem, cancel the failed rollout and schedule the same release again. Your enrollment remains valid.";
  if(!localEnrollment){
   restoreInstallation.Visible=true;start.Visible=false;pairing.Visible=false;start.Enabled=false;
   intro.Text=$"PC: {Environment.MachineName}\nThis PC was already approved. Its local enrollment files are missing. No new pairing is needed while the saved device key remains available.";
   enrollmentDetails.Text+="\nLocal installation: incomplete - agent credentials and supporting files need restoration.";
   status.Text=$"Approved: machine {machine} | Local enrollment missing | Restore approved installation";
   taskDetails.Text+="\nClick Restore approved installation to restore the agent. A failed initial installation of the same approved release will be queued again when safe.";
  }
 }
 async Task InstallPrerequisites(){
  var missing=Prerequisites();
  if(missing.Count==0){installPrerequisites.Visible=false;if(RecognizeEnrollment()){timer.Start();await Check();}else start.Enabled=canPair;return;}
  var message="Setup will install these Microsoft components:\n\n"+string.Join("\n",missing)+"\n\nMicrosoft license terms apply and will be accepted for these installations. Windows may ask for administrator approval. Setup will not restart Windows automatically. Continue?";
  if(MessageBox.Show(this,message,"Install Windows components",MessageBoxButtons.OKCancel,MessageBoxIcon.Information)!=DialogResult.OK)return;
  start.Enabled=false;installPrerequisites.Enabled=false;timer.Stop();
  try{
   bool reboot=await PrerequisiteInstaller.Install(value=>status.Text=value);
   if(reboot){status.Text="Windows requires a restart to finish this component installation. Restart Windows, then run PokerSuite Setup again; remaining components will be detected automatically.";return;}
   installPrerequisites.Visible=false;
   var enrolled=File.Exists(@"C:\PokerSuite\Updater\agent.protected")||File.Exists(@"C:\PokerSuite\Updater\agent.json");
   start.Enabled=canPair&&!enrolled;
   status.Text=enrolled?"Windows runtime repair completed. Your existing enrollment is preserved; no new pairing is needed. Close the PDecision error dialog. If the installation task has failed, cancel that rollout and schedule the same release again in Backoffice Updates.":"Windows components are ready. Click Get pairing code to continue.";
   if(enrolled){RecognizeEnrollment();timer.Start();await Check();}
   else if(requestId!=null){timer.Start();await Check();}
  }catch(System.ComponentModel.Win32Exception error) when(error.NativeErrorCode==1223){status.Text="Installation was cancelled at the administrator prompt. Click Install missing components to retry.";}
  catch(Exception error){status.Text=error.Message;}
  finally{installPrerequisites.Enabled=true;}
 }
 static string[] Addresses()=>PokerSuite.Shared.Utilities.MachineNetworkIdentity.GetMacAddresses();
 async Task<JsonElement> Post(string action,object body){using var content=new StringContent(JsonSerializer.Serialize(body),Encoding.UTF8,"application/json");using var response=await client.PostAsync(Base+"/api/setup/"+action,content);if(!response.IsSuccessStatusCode){var message=await response.Content.ReadAsStringAsync();try{message=JsonDocument.Parse(message).RootElement.GetProperty("error").GetString()!;}catch{message="Setup service returned "+(int)response.StatusCode;}throw new InvalidOperationException(message);}return (await response.Content.ReadFromJsonAsync<JsonElement>()).Clone();}
 async Task Begin(){
  if(RecognizeEnrollment()){timer.Start();await Check();return;}
  start.Enabled=false;
  try{
   var missing=Prerequisites();if(missing.Count>0)throw new InvalidOperationException("Install these Windows prerequisites first: "+string.Join(", ",missing));
   Directory.CreateDirectory(local);
   var path=Path.Combine(local,"device-key.protected");if(File.Exists(path))key.ImportFromPem(SecretStore.Read(path));else SecretStore.Write(path,key.ExportPkcs8PrivateKeyPem());
   var pending=Path.Combine(local,"request.json");
   if(File.Exists(pending)){var saved=JsonDocument.Parse(File.ReadAllText(pending)).RootElement;if(saved.GetProperty("expires").GetDateTimeOffset()>DateTimeOffset.UtcNow){requestId=saved.GetProperty("id").GetString();pairing.Text=saved.GetProperty("code").GetString();pairing.Visible=true;status.Text="Waiting for administrator approval...";timer.Start();return;}}
   var reply=await Post("request",new{hostname=Environment.MachineName,macs=Addresses(),public_key=key.ExportSubjectPublicKeyInfoPem()});
   File.WriteAllText(pending,reply.GetRawText());requestId=reply.GetProperty("id").GetString();pairing.Text=reply.GetProperty("code").GetString();pairing.Visible=true;status.Text="Waiting for administrator approval. The code expires in 30 minutes.";timer.Start();
  }catch(Exception error){status.Text=error.Message;start.Enabled=true;}
 }
 object Proof(string action){var timestamp=DateTimeOffset.UtcNow.ToUnixTimeSeconds();var nonce=Guid.NewGuid().ToString("N");var signature=Convert.ToBase64String(key.SignData(Encoding.UTF8.GetBytes($"{requestId}:{action}:{timestamp}:{nonce}"),HashAlgorithmName.SHA256,RSASignaturePadding.Pss));return new{id=requestId,timestamp,nonce,signature};}
 async Task Check(){if(busy)return;busy=true;try{
  if(EnrollmentPath()!=null){await MonitorEnrollment();return;}
  var reply=await Post("poll",Proof("poll"));var phase=reply.GetProperty("status").GetString();
  if(phase=="completed"){RenderStatus(reply.GetProperty("device_status"),false);timer.Start();return;}
  if(phase!="approved"){pairing.Visible=true;status.Text="Authentication: waiting for administrator approval | No application download yet";return;}
  var missing=Prerequisites();if(missing.Count>0)throw new InvalidOperationException("Install these Windows prerequisites, then setup will retry: "+string.Join(", ",missing));
  status.Text="Approved. Configuring this PC...";
  var package=Decrypt(reply.GetProperty("envelope"));await Install(package);
  await Post("complete",Proof("complete"));RecognizeEnrollment();timer.Start();await MonitorEnrollment();
 }catch(Exception error){status.Text=EnrollmentPath()!=null?"Enrollment preserved | Cannot refresh status: "+error.Message:error.Message+" - fix the issue and leave this window open to retry.";if(EnrollmentPath()==null&&error.Message.Contains("expired",StringComparison.OrdinalIgnoreCase)){timer.Stop();start.Enabled=true;start.Visible=true;pairing.Visible=false;}}finally{busy=false;}}
 static List<string> Prerequisites()=>PrerequisiteInstaller.Missing();
 JsonElement Decrypt(JsonElement envelope){
  var secret=key.Decrypt(Convert.FromBase64String(envelope.GetProperty("key").GetString()!),RSAEncryptionPadding.OaepSHA256);
  var ciphertext=Convert.FromBase64String(envelope.GetProperty("data").GetString()!);byte[] plaintext=new byte[ciphertext.Length-16];
  using(var aes=new AesGcm(secret,16))aes.Decrypt(Convert.FromBase64String(envelope.GetProperty("nonce").GetString()!),ciphertext.AsSpan(0,ciphertext.Length-16),ciphertext.AsSpan(ciphertext.Length-16),plaintext,Encoding.UTF8.GetBytes("PokerSuite-Pairing-v1"));
  return JsonDocument.Parse(plaintext).RootElement.Clone();
 }
 static async Task Run(string exe,IEnumerable<string> args){var info=new ProcessStartInfo(exe){UseShellExecute=false,CreateNoWindow=true,RedirectStandardOutput=true,RedirectStandardError=true};foreach(var arg in args)info.ArgumentList.Add(arg);using var process=Process.Start(info)!;var output=process.StandardOutput.ReadToEndAsync();var error=process.StandardError.ReadToEndAsync();await process.WaitForExitAsync();await output;await error;if(process.ExitCode!=0)throw new InvalidOperationException("Windows setup step failed. Check folder permissions and run setup as the intended Windows user.");}
 async Task Install(JsonElement package,bool restore=false){
  var cfg=package.GetProperty("config");int mid=cfg.GetProperty("MachineId").GetInt32();string root=cfg.GetProperty("Root").GetString()!;
  if(root!=@"C:\PokerSuite\Managed")throw new InvalidOperationException("Unexpected install location");
  if(File.Exists(Path.Combine(root,"operation.json")))throw new InvalidOperationException("An existing update needs recovery");
  var agent=@"C:\PokerSuite\Updater";Directory.CreateDirectory(root);Directory.CreateDirectory(agent);Directory.CreateDirectory(Path.Combine(root,"config"));
  var user=System.Security.Principal.WindowsIdentity.GetCurrent().Name;
  foreach(var folder in new[]{root,agent})await Run("icacls.exe",new[]{folder,"/inheritance:r","/grant:r",user+":(OI)(CI)F","SYSTEM:(OI)(CI)F"});
  if(File.Exists(Path.Combine(root,"active.json")))if(!restore)return;
  var worker=JsonSerializer.Deserialize<Dictionary<string,string>>(package.GetProperty("worker").GetRawText())!;
  using(var drivers=Microsoft.Win32.Registry.LocalMachine.OpenSubKey(@"SOFTWARE\ODBC\ODBCINST.INI\ODBC Drivers")){
   if(drivers?.GetValue("ODBC Driver 17 for SQL Server")==null){worker["SCHEDULE_ODBC_DRIVER"]="ODBC Driver 18 for SQL Server";if(worker.TryGetValue("SCHEDULE_SQL_CONNECTION_STRING",out var connection))worker["SCHEDULE_SQL_CONNECTION_STRING"]=connection.Replace("ODBC Driver 17 for SQL Server","ODBC Driver 18 for SQL Server",StringComparison.OrdinalIgnoreCase);}
  }
  SecretStore.Write(Path.Combine(agent,"agent.protected"),cfg.GetRawText());SecretStore.Write(Path.Combine(root,"config","pdecision.protected"),package.GetProperty("pdecision").GetRawText());SecretStore.Write(Path.Combine(root,"config","worker-env.protected"),JsonSerializer.Serialize(worker));
  if(Process.GetProcessesByName("PokerSuite.Updater").Length==0){
   using var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("PokerSuite.Setup.agent.zip")!;using var zip=new ZipArchive(stream);
   foreach(var entry in zip.Entries){if(entry.Name!=entry.FullName)throw new InvalidOperationException("Invalid embedded agent package");entry.ExtractToFile(Path.Combine(agent,entry.Name),true);}
  }
  var script=Path.Combine(local,"launch.ps1");
  File.WriteAllText(script,LaunchScript.Replace("MACHINE_ID",mid.ToString()));
  await Run("powershell.exe",new[]{"-NoProfile","-ExecutionPolicy","Bypass","-File",script});
 }
 const string LaunchScript="""
$ErrorActionPreference='Stop'
$exe='C:\PokerSuite\Updater\PokerSuite.Updater.exe'
$config='C:\PokerSuite\Updater\agent.protected'
$user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$action=New-ScheduledTaskAction -Execute $exe -Argument ('--config "'+$config+'"') -WorkingDirectory (Split-Path $exe)
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $user
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'PokerSuite Updater MACHINE_ID' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
if(-not (Get-Process PokerSuite.Updater -ErrorAction SilentlyContinue)){Start-Process -FilePath $exe -ArgumentList @('--config',('"'+$config+'"')) -WorkingDirectory (Split-Path $exe) -WindowStyle Hidden}
""";
}
