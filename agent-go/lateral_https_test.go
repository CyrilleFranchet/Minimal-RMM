package main

import "testing"

func TestLateralHTTPSValidation(t *testing.T) {
	for _, tc := range []struct {
		body          string
		secure, valid bool
	}{
		{`{"method":"winrm","host":"10.0.0.8","https":true}`, true, true},
		{`{"method":"winrm_https","host":"10.0.0.8"}`, true, true},
		{`{"method":"winrm","host":"10.0.0.8"}`, false, true},
		{`{"method":"winrm","host":"10.0.0.8","https":false}`, false, true},
		{`{"method":"wmiexec","host":"host","https":true}`, false, false},
		{`{"method":"psexec","host":"host","https":true}`, false, false},
		{`{"method":"winrm","host":"host","https":"true"}`, false, false},
		{`{"method":"winrm","host":"host","https":null}`, false, false},
		{`{"method":"winrm","host":"host","HTTPS":null}`, false, false},
	} {
		payload, err := parseLateralPayload("__LATERAL__\n" + tc.body)
		if (err == nil) != tc.valid || (err == nil && payload.HTTPS != tc.secure) {
			t.Fatalf("payload %s: secure=%v error=%v", tc.body, payload.HTTPS, err)
		}
		if err == nil && payload.HTTPS && payload.Method != "winrm" {
			t.Fatal("HTTPS wire method was not normalized")
		}
	}
}
