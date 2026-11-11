using System.IO.Compression;
using System.Net.Http.Json;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Diagnostics;

static class DiagnosticsCollector {
 internal const string Version="1.0.16";
 static readonly JsonSerializerOptions Json=new(JsonSerializerDefaults.Web);
 record Work(string Id,string Claim,int Hours,
  [property:System.Text.Json.Serialization.JsonPropertyName("upload_url")]string UploadUrl,
  [property:System.Text.Json.Serialization.JsonPropertyName("max_bytes")]long MaxBytes);
 record Reply(Work? Request);
 static readonly HttpClient Api=new(){Timeout=TimeSpan.FromSeconds(90)};
 static async Task<T> Post<T>(Config c,string action,object body){
  using var req=new HttpRequestMessage(HttpMethod.Post,$"{c.BaseUrl.TrimEnd('/')}/api/updater/{c.MachineId}/{action}");
  req.Headers.Add("X-Updater-Token",c.Token);req.Content=new StringContent(JsonSerializer.Serialize(body,Json),Encoding.UTF8,"application/json");
  using var response=await Api.SendAsync(req);response.EnsureSuccessStatusCode();return (await response.Content.ReadFromJsonAsync<T>(Json))!;
 }
 internal static async Task Run(Config c,int? parent=null){
  using var owner=parent.HasValue?Process.GetProcessById(parent.Value):null;
  // Both the installed updater and the bundled helper can run on the same PC.
  // Hold an OS file handle across awaits so only one polls for diagnostic work.
  FileStream? lease=null;
  Directory.CreateDirectory(c.Root);
  while(lease==null&&(owner==null||!owner.HasExited)){
   try{lease=new FileStream(Path.Combine(c.Root,"diagnostics.collector.lock"),FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None);}
   catch(IOException){await Task.Delay(15000);}
  }
  if(lease==null)return;
  using var held=lease;
  while(owner==null||!owner.HasExited){
   try{await Once(c);}catch(Exception error){
    try{File.AppendAllText(Path.Combine(c.Root,"diagnostics.log"),$"{DateTime.UtcNow:O} Collection retry: {error.GetType().Name}"+(error is HttpRequestException http?$" HTTP {(int?)http.StatusCode}":"")+"\n");}catch{}
   }
   await Task.Delay(15000);
  }
 }
 static async Task Once(Config c){
  var reply=await Post<Reply>(c,"logs-poll",new{collector_version=Version});var work=reply.Request;if(work==null)return;
  var path=Path.Combine(c.Root,"diagnostics",Guid.Parse(work.Id)+"-"+Guid.Parse(work.Claim)+".zip");Directory.CreateDirectory(Path.GetDirectoryName(path)!);path=Safe(path,c.Root);
  try{
   await Task.Run(()=>Collect(c,work.Hours,path,Math.Min(work.MaxBytes,64L*1024*1024)));
   await Post<JsonElement>(c,"logs-report",new{collector_version=Version,id=work.Id,claim=work.Claim,phase="uploading"});
   var url=new Uri(work.UploadUrl);
   if(url.Scheme!="https"||!url.Host.EndsWith(".blob.core.windows.net",StringComparison.OrdinalIgnoreCase)||!string.IsNullOrEmpty(url.UserInfo))throw new InvalidDataException("Invalid diagnostic upload destination");
   using var input=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read);
   var digest=Convert.ToHexString(await SHA256.HashDataAsync(input)).ToLowerInvariant();input.Position=0;
   using var handler=new HttpClientHandler{AllowAutoRedirect=false};using var upload=new HttpClient(handler){Timeout=TimeSpan.FromMinutes(5)};
   using var req=new HttpRequestMessage(HttpMethod.Put,url){Content=new StreamContent(input)};
   req.Headers.Add("x-ms-blob-type","BlockBlob");req.Content.Headers.ContentType=new("application/zip");
   using var response=await upload.SendAsync(req);response.EnsureSuccessStatusCode();
   await Post<JsonElement>(c,"logs-report",new{collector_version=Version,id=work.Id,claim=work.Claim,phase="ready",size_bytes=input.Length,sha256=digest});
  }catch{
   try{await Post<JsonElement>(c,"logs-report",new{collector_version=Version,id=work.Id,claim=work.Claim,phase="failed"});}catch{}
   throw;
  }finally{try{File.Delete(path);}catch{}}
 }
 static string Safe(string path,string root){
  var full=Path.GetFullPath(path);var parent=Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar)+Path.DirectorySeparatorChar;
  if(!full.StartsWith(parent,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("Outside diagnostic roots");
  for(var current=new FileInfo(full) as FileSystemInfo;current!=null;current=current is FileInfo f?f.Directory:((DirectoryInfo)current).Parent){
   if(current.Exists&&(current.Attributes&FileAttributes.ReparsePoint)!=0)throw new InvalidDataException("Diagnostic links are not allowed");
   if(string.Equals(current.FullName,Path.GetFullPath(root),StringComparison.OrdinalIgnoreCase))break;
  }
  return full;
 }
 static List<string> Secrets(Config c){
  var result=new List<string>{c.Token};
  void Find(JsonElement e,string key=""){
   if(e.ValueKind==JsonValueKind.Object){foreach(var p in e.EnumerateObject())Find(p.Value,p.Name);}
   else if(e.ValueKind==JsonValueKind.String&&Regex.IsMatch(key,"password|token|secret|api_?key|connectionstring|sql_connection",RegexOptions.IgnoreCase)){
    var value=e.GetString();if(!string.IsNullOrEmpty(value)){
     result.Add(value);
     foreach(Match m in Regex.Matches(value,@"(?:Password|Pwd|AccountKey)\s*=\s*([^;]+)",RegexOptions.IgnoreCase))result.Add(m.Groups[1].Value);
    }
   }
  }
  foreach(var name in new[]{"pdecision.protected","pdecision.json","worker-env.protected","worker-env.json"}){
   var path=Path.Combine(c.Root,"config",name);if(!File.Exists(path))continue;
   using var doc=JsonDocument.Parse(name.EndsWith(".protected")?SecretStore.Read(path):File.ReadAllText(path));Find(doc.RootElement);
  }
  return result.Distinct().OrderByDescending(x=>x.Length).ToList();
 }
 internal static string Redact(string text,IEnumerable<string> secrets){
  foreach(var secret in secrets)if(secret.Length>0)text=text.Replace(secret,"[redacted]",StringComparison.OrdinalIgnoreCase);
  text=Regex.Replace(text,@"https?://[^\s\""<>]+\?[^\s\""<>]+","[redacted URL]",RegexOptions.IgnoreCase);
  text=Regex.Replace(text,@"(?i)((?:password|pwd|token|secret|apikey|api_key|accountkey|authorization)\s*[=:]\s*)[^;\s,]+","$1[redacted]");
  return text;
 }
 internal static void Collect(Config c,int hours,string destination,long maxBytes){
  if(hours<1||hours>168)throw new InvalidDataException("Invalid log history");
  var suite=Path.GetDirectoryName(Path.GetFullPath(c.Root))!;var cutoff=DateTime.UtcNow.AddHours(-hours);
  var files=new List<(string Path,string Name)>();
  void Add(string path,string root,string name){try{if(File.Exists(path)&&File.GetLastWriteTimeUtc(path)>=cutoff)files.Add((Safe(path,root),name));}catch{}}
  Add(Path.Combine(c.Root,"updater.log"),c.Root,"updater/updater.log");
  Add(Path.Combine(c.Root,"diagnostics.log"),c.Root,"updater/diagnostics.log");
  var logs=Path.Combine(suite,"Logs");
  if(Directory.Exists(logs))foreach(var file in Directory.EnumerateFiles(logs,"PDecision*.log*",SearchOption.TopDirectoryOnly).OrderByDescending(File.GetLastWriteTimeUtc).Take(20))Add(file,logs,"pdecision/"+Path.GetFileName(file));
  var runs=Path.Combine(c.Root,"runs");
  if(Directory.Exists(runs))foreach(var folder in Directory.EnumerateDirectories(runs).OrderByDescending(Directory.GetLastWriteTimeUtc).Take(50)){
   foreach(var name in new[]{"worker.log","pilot.log","status.json"})Add(Path.Combine(folder,name),runs,"controller/"+Path.GetFileName(folder)+"/"+name);
  }
  var secrets=Secrets(c);var manifest=new List<object>();long remaining=48L*1024*1024;
  using(var zip=ZipFile.Open(destination,ZipArchiveMode.Create)){
   foreach(var file in files){
    try{
     using var stream=new FileStream(file.Path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete);
     var limit=(int)Math.Min(8L*1024*1024,remaining);if(limit<=0)break;
     var original=stream.Length;stream.Position=Math.Max(0,original-limit);var offset=stream.Position;
     var bytes=new byte[Math.Min(original-offset,limit)];int used=0,n;while(used<bytes.Length&&(n=stream.Read(bytes,used,bytes.Length-used))>0)used+=n;
     var text=Encoding.UTF8.GetString(bytes,0,used);if(offset>0&&text.Contains('\n'))text=text[(text.IndexOf('\n')+1)..];
     text=Redact(text,secrets);var entry=zip.CreateEntry(file.Name,CompressionLevel.Fastest);using(var writer=new StreamWriter(entry.Open(),new UTF8Encoding(false)))writer.Write(text);
     remaining-=used;manifest.Add(new{file=file.Name,original_bytes=original,included_bytes=used,truncated=offset>0,modified_utc=File.GetLastWriteTimeUtc(file.Path)});
    }catch{manifest.Add(new{file=file.Name,skipped="File unavailable"});}
   }
   object? applications=null;
   try{
    var active=JsonSerializer.Deserialize<Apps>(File.ReadAllText(Path.Combine(c.Root,"active.json")),Json)!;
    string? decisionVersion=null;try{decisionVersion=FileVersionInfo.GetVersionInfo(Safe(active.DecisionExe,c.Root)).FileVersion;}catch{}
    applications=new{installed_release_id=active.ReleaseId,pdecision_version=decisionVersion,decision_pid=active.DecisionPid,worker_pid=active.WorkerPid};
   }catch{}
   var summary=new{machine_id=c.MachineId,site_id=c.SiteId,hostname=Environment.MachineName,os=Environment.OSVersion.ToString(),collector_version=Version,applications,captured_utc=DateTime.UtcNow,hours,
    selection="Recent files modified within the requested hours; bounded tails, up to 8 MiB per file and 48 MiB total. No settings or enrollment files included.",files=manifest};
   using var summaryWriter=new StreamWriter(zip.CreateEntry("manifest.json").Open());summaryWriter.Write(JsonSerializer.Serialize(summary,new JsonSerializerOptions{WriteIndented=true}));
  }
  if(new FileInfo(destination).Length>maxBytes)throw new InvalidDataException("Diagnostic package exceeds size limit");
 }
 internal static void SelfTest(){
  var suite=Path.Combine(Path.GetTempPath(),"PokerSuite-diagnostics-"+Guid.NewGuid());var root=Path.Combine(suite,"Managed");Directory.CreateDirectory(Path.Combine(root,"runs","test"));Directory.CreateDirectory(Path.Combine(root,"config"));Directory.CreateDirectory(Path.Combine(suite,"Logs"));
  var token="unique-test-token-value";var secret="unique-test-password-value";
  File.WriteAllText(Path.Combine(root,"config","worker-env.json"),JsonSerializer.Serialize(new{SCHEDULE_SQL_CONNECTION_STRING=$"Server=example;Password={secret};"}));
  File.WriteAllText(Path.Combine(root,"runs","test","worker.log"),$"password={secret}\n{token}\nhttps://test.blob.core.windows.net/a?sig=hidden\nConnection lost\n");
  File.WriteAllText(Path.Combine(suite,"Logs","PDecision.log"),"PDecision ready");
  var config=new Config("https://example.invalid",1,1,token,[],root,"http://localhost/");var path=Path.Combine(suite,"test.zip");Collect(config,24,path,64*1024*1024);
  using(var zip=ZipFile.OpenRead(path)){
   var all=string.Join("\n",zip.Entries.Select(e=>{using var r=new StreamReader(e.Open());return r.ReadToEnd();}));
   if(all.Contains(token)||all.Contains(secret)||all.Contains("sig=hidden")||!all.Contains("Connection lost")||zip.Entries.Any(e=>e.Name.Contains("worker-env")))throw new Exception("Diagnostics redaction or allowlist failed");
  }
  bool blocked=false;try{Safe(Path.Combine(suite,"outside.log"),root);}catch(InvalidDataException){blocked=true;}if(!blocked)throw new Exception("Diagnostic path escape accepted");
  Directory.Delete(suite,true);
 }
}
