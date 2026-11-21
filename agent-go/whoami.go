package main

import "fmt"

// whoamiPrivilegeCatalog lists the documented Windows token privileges.
var whoamiPrivilegeCatalog = []string{
	"SeAssignPrimaryTokenPrivilege", "SeAuditPrivilege", "SeBackupPrivilege", "SeChangeNotifyPrivilege",
	"SeCreateGlobalPrivilege", "SeCreatePagefilePrivilege", "SeCreatePermanentPrivilege", "SeCreateSymbolicLinkPrivilege",
	"SeCreateTokenPrivilege", "SeDebugPrivilege", "SeDelegateSessionUserImpersonatePrivilege", "SeEnableDelegationPrivilege",
	"SeImpersonatePrivilege", "SeIncreaseBasePriorityPrivilege", "SeIncreaseQuotaPrivilege", "SeIncreaseWorkingSetPrivilege",
	"SeLoadDriverPrivilege", "SeLockMemoryPrivilege", "SeMachineAccountPrivilege", "SeManageVolumePrivilege",
	"SeProfileSingleProcessPrivilege", "SeRelabelPrivilege", "SeRemoteShutdownPrivilege", "SeRestorePrivilege",
	"SeSecurityPrivilege", "SeShutdownPrivilege", "SeSyncAgentPrivilege", "SeSystemEnvironmentPrivilege",
	"SeSystemProfilePrivilege", "SeSystemtimePrivilege", "SeTakeOwnershipPrivilege", "SeTcbPrivilege",
	"SeTimeZonePrivilege", "SeTrustedCredManAccessPrivilege", "SeUndockPrivilege", "SeUnsolicitedInputPrivilege",
}

// whoamiPrivilegeState preserves the distinction between absent and disabled.
func whoamiPrivilegeState(attributes uint32, present bool) string {
	if !present {
		return "absent"
	}
	state := "disabled"
	if attributes&2 != 0 {
		state = "enabled"
	}
	if attributes&4 != 0 {
		state = "removed"
	}
	return fmt.Sprintf("%s (default=%t, used-for-access=%t, attributes=0x%08x)", state, attributes&1 != 0, attributes&0x80000000 != 0, attributes)
}

// whoamiScalar accepts native BOOLEAN only for TokenHasRestrictions; other
// token scalar classes return DWORD values and require four bytes.
func whoamiScalar(class uint32, data []byte) (uint32, error) {
	if class == 21 && len(data) >= 1 {
		return uint32(data[0]), nil
	}
	if len(data) < 4 {
		return 0, fmt.Errorf("token information class %d: short scalar buffer (%d bytes)", class, len(data))
	}
	return uint32(data[0]) | uint32(data[1])<<8 | uint32(data[2])<<16 | uint32(data[3])<<24, nil
}
