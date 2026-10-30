using System.Text.Json;

record InstalledEnrollment(int MachineId,int SiteId,string Token,string BaseUrl,string Root){
 internal static string? Find(string folder=@"C:\PokerSuite\Updater"){
  foreach(var name in new[]{"agent.protected","agent.json"}){
   var path=Path.Combine(folder,name);
   try{File.GetAttributes(path);return path;}
   catch(UnauthorizedAccessException){return path;}
   catch(FileNotFoundException){}
   catch(DirectoryNotFoundException){}
  }
  return null;
 }
 internal static InstalledEnrollment Load(string path){
  var text=path.EndsWith(".protected",StringComparison.OrdinalIgnoreCase)?SecretStore.Read(path):File.ReadAllText(path);
  var value=JsonSerializer.Deserialize<InstalledEnrollment>(text,new JsonSerializerOptions(JsonSerializerDefaults.Web))??throw new InvalidDataException("Enrollment is empty");
  if(value.MachineId<1||value.SiteId<1||string.IsNullOrEmpty(value.Token)||value.Token.Length<40||!Uri.TryCreate(value.BaseUrl,UriKind.Absolute,out var url)||url.Scheme!="https")throw new InvalidDataException("Enrollment is invalid");
  return value;
 }
 internal static string Describe(JsonElement value){
  var id=value.GetProperty("machine_id").GetInt32();var online=value.GetProperty("agent_online").GetBoolean();
  var update=value.GetProperty("update");var phase=update.ValueKind==JsonValueKind.Object?update.GetProperty("status").GetString():null;
  var action=phase switch {"queued"=>"Waiting to install","preparing"=>"Downloading and verifying release","draining"=>"Finishing active sessions","stopping"=>"Preparing application switch","installing"=>"Installing applications","verifying"=>"Starting and checking applications","resuming"=>"Applications started; recovering traffic","completed"=>"Up to date","failed"=>"Installation failed","rolled_back"=>"Previous release restored",_=>phase?.Replace('_',' ')};
  return $"Authenticated: machine {id} | Updater {(online?"connected":"not connected")}"+(action==null?"":$" | {action}");
 }
 internal static void SelfTest(){
  var folder=Path.Combine(Path.GetTempPath(),"PokerSuite-enrollment-check-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(folder);
  var json=JsonSerializer.Serialize(new InstalledEnrollment(10,1,new string('x',48),"https://example.invalid",@"C:\PokerSuite\Managed"));
  File.WriteAllText(Path.Combine(folder,"request.json"),"{\"status\":\"completed\"}");
  File.WriteAllText(Path.Combine(folder,"agent.json"),json);
  if(Load(Find(folder)!).MachineId!=10)throw new Exception("Legacy enrollment was not recognized");
  SecretStore.Write(Path.Combine(folder,"agent.protected"),json);
  if(!Find(folder)!.EndsWith(".protected")||Load(Find(folder)!).MachineId!=10)throw new Exception("Protected enrollment was not recognized with old pairing file present");
  var status=JsonDocument.Parse("{\"machine_id\":10,\"agent_online\":true,\"update\":{\"status\":\"failed\"}}").RootElement;
  if(!Describe(status).Contains("Authenticated: machine 10")||!Describe(status).Contains("failed"))throw new Exception("Enrollment status display failed");
 }
}
