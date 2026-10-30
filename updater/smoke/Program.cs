using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
class InvisibleForm:Form {protected override bool ShowWithoutActivation=>true;public InvisibleForm(){Text="Updater smoke-test window";Opacity=0;ShowInTaskbar=false;WindowState=FormWindowState.Minimized;}}
static class Program {
 [STAThread]static void Main(string[] args){
  var root=AppContext.BaseDirectory;var exe=Path.GetFileName(Environment.ProcessPath)!;
  var build=JsonDocument.Parse(File.ReadAllText(Path.Combine(root,"build-info.json"))).RootElement;
  var process=Process.GetCurrentProcess();File.WriteAllText(Path.Combine(root,exe+".started.json"),JsonSerializer.Serialize(new{pid=process.Id,started=process.StartTime.ToUniversalTime().Ticks,protectedWorker=Environment.GetEnvironmentVariable("SqlConnectionString")=="dummy-protected-worker",protectedDecision=Environment.GetEnvironmentVariable("PDECISION_Database__ConnectionString")=="dummy-protected-decision"}));
  if(exe=="PokerController.exe"){
   var index=Array.IndexOf(args,"--output");var output=args[index+1];Directory.CreateDirectory(output);
   File.WriteAllText(Path.Combine(output,"status.json"),JsonSerializer.Serialize(new{controller_version=build.GetProperty("controllerVersion").GetString()}));
   while(!File.Exists(Path.Combine(output,"stop")))Thread.Sleep(100);return;
  }
  if(build.TryGetProperty("fail",out var fail)&&fail.GetBoolean())return;
  var settings=JsonDocument.Parse(File.ReadAllText(Path.Combine(root,"appsettings.json"))).RootElement;
  var listener=new TcpListener(IPAddress.Loopback,int.TryParse(Environment.GetEnvironmentVariable("PDECISION_Port"),out var injectedPort)?injectedPort:settings.GetProperty("Port").GetInt32());listener.Start();
  _=Task.Run(async()=>{while(true){using var client=await listener.AcceptTcpClientAsync();using var stream=client.GetStream();using var reader=new StreamReader(stream,Encoding.ASCII,false,1024,true);string? line;do{line=await reader.ReadLineAsync();}while(!string.IsNullOrEmpty(line));var body=JsonSerializer.Serialize(new{status="ready",version=build.GetProperty("pdecisionVersion").GetString()});var bytes=Encoding.UTF8.GetBytes(body);var headers=Encoding.ASCII.GetBytes($"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {bytes.Length}\r\nConnection: close\r\n\r\n");await stream.WriteAsync(headers);await stream.WriteAsync(bytes);}});
  Application.Run(new InvisibleForm());listener.Stop();
 }
}
