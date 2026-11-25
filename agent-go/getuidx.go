package main

import "fmt"

// getuidxAction provides dual-identity inspection: the local process identity,
// the configured make_token network identity, and the effective thread token's
// logon session details. This surfaces the type-9 logon trap where local !=
// network identity — a spawned child from echelon 1/2 authenticates as the
// alternate identity on the network while running locally as the agent user.
//
// Output contract:
//
//	process identity: DOMAIN\user
//	network identity: DOMAIN\alternate | none
//	logon type: 9 (NewCredentials) | ...
//	logon session identity: DOMAIN\user
//	identity split: true | false
//
// The "identity split" line is the key signal: true means local commands run
// as the agent user but network traffic authenticates as the alternate.
func getuidxAction() (string, error) {
	return getuidxPlatform()
}

// getuidxLabel renders a one-line summary for operator display.
func getuidxLabel(processUser, networkIdentity string, split bool) string {
	if !split || networkIdentity == "" {
		return fmt.Sprintf("process identity: %s\nnetwork identity: none (commands use agent identity)\nidentity split: false",
			processUser)
	}
	return fmt.Sprintf("process identity: %s\nnetwork identity: %s\nidentity split: true (local commands run as %s, network traffic authenticates as %s)",
		processUser, networkIdentity, processUser, networkIdentity)
}
