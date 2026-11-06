//go:build windows

package main

import (
	"syscall"
	"unsafe"
)

// stage1 runs as a proper Windows service: it connects to the SCM, reports
// its staging work as the service body, and stops cleanly when the work is
// done, so the operator can delete the service right after the agent runs.

const (
	stageServiceWin32OwnProcess = 0x10
	stageServiceStartPending    = 2
	stageServiceRunning         = 4
	stageServiceStopped         = 1
	stageServiceAcceptStop      = 1
	stageServiceControlStop     = 1
)

var (
	stageAdvapi32        = syscall.NewLazyDLL("advapi32.dll")
	procStartDispatcherW = stageAdvapi32.NewProc("StartServiceCtrlDispatcherW")
	procRegisterHandlerW = stageAdvapi32.NewProc("RegisterServiceCtrlHandlerW")
	procSetServiceStatus = stageAdvapi32.NewProc("SetServiceStatus")
)

type stageTableEntry struct {
	name *uint16
	proc uintptr
}

type stageStatus struct {
	serviceType             uint32
	currentState            uint32
	controlsAccepted        uint32
	win32ExitCode           uint32
	serviceSpecificExitCode uint32
	checkPoint              uint32
	waitHint                uint32
}

var stageWork func() error
var stageStop = make(chan struct{})

// runStageAsService connects to the SCM when this process was started as a
// service (the psexec module). It returns false for normal starts.
func runStageAsService(work func() error) bool {
	name, err := syscall.UTF16PtrFromString("MinimalRmmStage1")
	if err != nil {
		return false
	}
	stageWork = work
	table := []stageTableEntry{{name: name, proc: syscall.NewCallback(stageServiceMain)}, {}}
	result, _, _ := procStartDispatcherW.Call(uintptr(unsafe.Pointer(&table[0])))
	return result != 0
}

func stageServiceMain(_ uintptr, _ uintptr) uintptr {
	status := stageStatus{
		serviceType:  stageServiceWin32OwnProcess,
		currentState: stageServiceStartPending,
		waitHint:     30000,
	}
	procSetServiceStatus.Call(uintptr(unsafe.Pointer(&status)))
	name, _ := syscall.UTF16PtrFromString("MinimalRmmStage1")
	procRegisterHandlerW.Call(uintptr(unsafe.Pointer(name)), syscall.NewCallback(stageControlHandler))
	status.currentState = stageServiceRunning
	status.controlsAccepted = stageServiceAcceptStop
	procSetServiceStatus.Call(uintptr(unsafe.Pointer(&status)))
	if stageWork != nil {
		_ = stageWork()
	}
	status.currentState = stageServiceStopped
	status.controlsAccepted = 0
	procSetServiceStatus.Call(uintptr(unsafe.Pointer(&status)))
	return 0
}

func stageControlHandler(control uintptr) uintptr {
	_ = control // stage1 completes in seconds; stop requests are not expected
	return 0
}
