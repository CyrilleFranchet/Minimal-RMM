package main

import "fmt"

// uidAction reports the agent's process identity and the active network
// identity so operators can verify make_token and rev2self. The output is a
// stable two-line contract consumed by the uid operator surfaces.
func uidAction() (string, error) {
	agentUser := processUserIdentity()
	identity := activeNetworkIdentity()
	if identity == "" {
		return fmt.Sprintf(
			"agent user: %s\nnetwork identity: none (spawned commands use the agent identity)",
			agentUser,
		), nil
	}
	return fmt.Sprintf(
		"agent user: %s\nnetwork identity: %s (make_token active; spawned commands authenticate as this identity on the network)",
		agentUser, identity,
	), nil
}
