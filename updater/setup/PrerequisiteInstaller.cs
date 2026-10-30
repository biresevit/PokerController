using System.Diagnostics;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.Win32;

record PrerequisitePackage(string Id,string Label,string File,string Sha256,long Size,string Kind,string[] Args,string? MinimumVersion=null);

static class PrerequisiteInstaller {
 static readonly JsonSerializerOptions Json=new(JsonSerializerDefaults.Web);
 static Stream Resource(string name)=>Assembly.GetExecutingAssembly().GetManifestResourceStream("Prerequisites."+name)??throw new InvalidOperationException("Bundled installer is missing: "+name);
 static List<PrerequisitePackage> Packages(){using var stream=Resource("manifest.json");return JsonSerializer.Deserialize<List<PrerequisitePackage>>(stream,Json)!;}
 public static List<string> Missing(){
  var missing=new List<string>();var shared=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),"dotnet","shared");
  foreach(var pair in new[]{("runtime","Microsoft.NETCore.App","coreclr.dll"),("desktop","Microsoft.WindowsDesktop.App","PresentationFramework.dll"),("aspnet","Microsoft.AspNetCore.App","Microsoft.AspNetCore.dll")}){
   var package=Packages().Single(p=>p.Id==pair.Item1);var minimum=Version.Parse(package.MinimumVersion!);
   var path=Path.Combine(shared,pair.Item2);
   if(!Directory.Exists(path)||!Directory.EnumerateDirectories(path).Any(folder=>Compatible(Path.GetFileName(folder),minimum)&&File.Exists(Path.Combine(folder,pair.Item3))))missing.Add(package.Label+" ("+minimum+" or later 8.0 patch)");
  }
  using var localdb=Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Microsoft SQL Server Local DB\Installed Versions");if(localdb==null||localdb.SubKeyCount==0)missing.Add("SQL Server Express LocalDB x64");
  using var drivers=Registry.LocalMachine.OpenSubKey(@"SOFTWARE\ODBC\ODBCINST.INI\ODBC Drivers");if(drivers?.GetValue("ODBC Driver 17 for SQL Server")==null&&drivers?.GetValue("ODBC Driver 18 for SQL Server")==null)missing.Add("SQL Server ODBC Driver 17 or 18 x64");
  using var vc=Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64");if(vc?.GetValue("Installed") is not int installed||installed!=1)missing.Add("Microsoft Visual C++ Runtime x64");
  return missing;
 }
 internal static bool Compatible(string version,Version minimum)=>Version.TryParse(version,out var installed)&&installed.Major==minimum.Major&&installed.Minor==minimum.Minor&&installed>=minimum;
 static bool Includes(IEnumerable<string> missing,PrerequisitePackage package)=>missing.Any(label=>label==package.Label||label.StartsWith(package.Label+" (",StringComparison.Ordinal));
 internal static IEnumerable<PrerequisitePackage> Plan(IEnumerable<string> missing)=>Packages().Where(p=>Includes(missing,p)).OrderBy(p=>p.Id=="vc"?0:p.Id=="runtime"?1:p.Id=="desktop"?2:p.Id=="aspnet"?3:p.Id=="localdb"?4:5);
 static async Task<string> Extract(PrerequisitePackage package,string folder){
  if(Path.GetFileName(package.File)!=package.File)throw new InvalidDataException("Invalid bundled installer path");
  var path=Path.Combine(folder,package.File);
  using(var input=Resource(package.File)){await using var output=File.Create(path);await input.CopyToAsync(output);}
  await using(var stream=File.OpenRead(path)){
   if(stream.Length!=package.Size||!Convert.ToHexString(await SHA256.HashDataAsync(stream)).Equals(package.Sha256,StringComparison.OrdinalIgnoreCase))throw new InvalidDataException("Bundled installer checksum mismatch: "+package.Label);
  }
  // Verify the signed Microsoft publisher before elevation. The file stays in a
  // unique, current-user directory; other ordinary users cannot replace it.
  var script="$s=Get-AuthenticodeSignature -LiteralPath '"+path.Replace("'","''")+"';if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch '(^|,\\s*)O=Microsoft Corporation(,|$)'){exit 1}";
  var info=new ProcessStartInfo("powershell.exe"){UseShellExecute=false,CreateNoWindow=true};info.ArgumentList.Add("-NoProfile");info.ArgumentList.Add("-EncodedCommand");info.ArgumentList.Add(Convert.ToBase64String(System.Text.Encoding.Unicode.GetBytes(script)));
  using var verify=Process.Start(info)!;await verify.WaitForExitAsync();if(verify.ExitCode!=0)throw new InvalidDataException("Microsoft signature verification failed: "+package.Label);
  return path;
 }
 public static async Task<bool> Install(Action<string> progress){
  var folder=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"PokerSuite","Prerequisites",Guid.NewGuid().ToString("N"));Directory.CreateDirectory(folder);
  foreach(var package in Plan(Missing()).ToList()){
   if(!Includes(Missing(),package))continue;
   progress("Verifying "+package.Label+"...");var path=await Extract(package,folder);
   progress("Installing "+package.Label+". Approve the Windows administrator prompt if shown.");
   var info=new ProcessStartInfo(package.Kind=="msi"?Path.Combine(Environment.SystemDirectory,"msiexec.exe"):path){UseShellExecute=true,Verb="runas"};
   if(package.Kind=="msi"){info.ArgumentList.Add("/i");info.ArgumentList.Add(path);info.ArgumentList.Add("/passive");info.ArgumentList.Add("/norestart");info.ArgumentList.Add("/L*v");info.ArgumentList.Add(Path.Combine(folder,package.Id+".log"));}
   foreach(var arg in package.Args)info.ArgumentList.Add(arg);
   using var process=Process.Start(info)??throw new InvalidOperationException("Installer did not start");await process.WaitForExitAsync();
   if(process.ExitCode is 3010 or 1641)return true;
   if(process.ExitCode!=0)throw new InvalidOperationException(package.Label+" installation failed (code "+process.ExitCode+"). Installer logs: "+folder);
  }
  var remaining=Missing();if(remaining.Count>0)throw new InvalidOperationException("Installation finished but Windows still reports missing: "+string.Join(", ",remaining)+". Restart Windows and retry setup.");
  return false;
 }
 public static async Task SelfTest(){
  var all=Packages();if(all.Count!=6||Plan(Array.Empty<string>()).Any()||Plan(new[]{"ASP.NET Core 8 Runtime x64"}).Single().Id!="aspnet")throw new Exception("Prerequisite selection failed");
  var minimum=Version.Parse(all.Single(p=>p.Id=="runtime").MinimumVersion!);
  if(Compatible("8.0.28",minimum)||!Compatible(minimum.ToString(),minimum)||!Compatible("8.0.99",minimum)||Compatible("9.0.33",minimum)||Compatible("8.0.33-preview",minimum))throw new Exception("Runtime patch validation failed");
  if(Plan(new[]{".NET 8 Runtime x64 ("+minimum+" or later 8.0 patch)"}).Single().Id!="runtime")throw new Exception("Missing base runtime was not selected");
  var folder=Path.Combine(Path.GetTempPath(),"PokerSuite-package-check-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(folder);
  foreach(var package in all)await Extract(package,folder);
  File.WriteAllText(Path.Combine(folder,"verified.txt"),"All bundled Microsoft signatures and checksums verified. No software was installed.");
 }
}
