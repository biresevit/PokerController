using System.Runtime.InteropServices;
using System.Text;
static class SecretStore {
 [StructLayout(LayoutKind.Sequential)]struct Blob {public int Size;public IntPtr Data;}
 [DllImport("crypt32.dll",SetLastError=true,CharSet=CharSet.Unicode)]static extern bool CryptProtectData(ref Blob data,string description,IntPtr entropy,IntPtr reserved,IntPtr prompt,int flags,out Blob output);
 [DllImport("crypt32.dll",SetLastError=true,CharSet=CharSet.Unicode)]static extern bool CryptUnprotectData(ref Blob data,IntPtr description,IntPtr entropy,IntPtr reserved,IntPtr prompt,int flags,out Blob output);
 [DllImport("kernel32.dll")]static extern IntPtr LocalFree(IntPtr memory);
 static byte[] Transform(byte[] bytes,bool protect){
  var input=new Blob{Size=bytes.Length,Data=Marshal.AllocHGlobal(bytes.Length)};Blob output=default;
  try {Marshal.Copy(bytes,0,input.Data,bytes.Length);
   bool ok=protect?CryptProtectData(ref input,"PokerSuite",IntPtr.Zero,IntPtr.Zero,IntPtr.Zero,1,out output):CryptUnprotectData(ref input,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero,1,out output);
   if(!ok)throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
   byte[] result=new byte[output.Size];Marshal.Copy(output.Data,result,0,result.Length);return result;
  }finally{Marshal.FreeHGlobal(input.Data);if(output.Data!=IntPtr.Zero)LocalFree(output.Data);}
 }
 public static void Write(string path,string value){File.WriteAllBytes(path+".tmp",Transform(Encoding.UTF8.GetBytes(value),true));File.Move(path+".tmp",path,true);}
 public static string Read(string path)=>Encoding.UTF8.GetString(Transform(File.ReadAllBytes(path),false));
}
