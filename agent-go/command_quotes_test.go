package main

import (
	"reflect"
	"testing"
)

func TestNormalizeCMDQuotes(t *testing.T) {
	for _, tt := range []struct{ name, input, want string }{
		{"group name", `net group 'Domain Admins' /domain`, `net group "Domain Admins" /domain`},
		{"multiple arguments", `echo 'one two' 'three four'`, `echo "one two" "three four"`},
		{"empty argument", `echo ''`, `echo ""`},
		{"quoted path", `'C:\Program Files\tool.exe' 'two words'`, `"C:\Program Files\tool.exe" "two words"`},
		{"shell operators", `echo 'one & two'|find 'one'`, `echo "one & two"|find "one"`},
		{"literal apostrophes", `echo don't change O'Brien`, `echo don't change O'Brien`},
		{"apostrophe inside argument", `echo 'O'Brien Team'`, `echo 'O'Brien Team'`},
		{"existing double quotes", `echo "'two words'"`, `echo "'two words'"`},
		{"mixed quotes", `echo "keep 'these'" 'convert these'`, `echo "keep 'these'" "convert these"`},
		{"unmatched quote", `echo 'two words`, `echo 'two words`},
		{"embedded double quote", `echo 'two "words"'`, `echo 'two "words"'`},
		{"escaped quote", `echo ^'two words'`, `echo ^'two words'`},
		{"escaped content", `echo 'one ^& two'`, `echo 'one ^& two'`},
		{"partial argument", `echo prefix'two words'suffix`, `echo prefix'two words'suffix`},
		{"partial close before command", `echo 'foo'bar & echo 'baz'`, `echo 'foo'bar & echo "baz"`},
		{"ambiguous double quote state", `echo 'one "two' 'three'`, `echo 'one "two' 'three'`},
	} {
		t.Run(tt.name, func(t *testing.T) {
			if got := normalizeCMDQuotes(tt.input); got != tt.want {
				t.Fatalf("got %q; want %q", got, tt.want)
			}
		})
	}
}

func TestBuildCommandInvocationQuoteNormalization(t *testing.T) {
	for _, prefix := range []string{"", "cmd: "} {
		_, args, err := buildCommandInvocation(prefix+`echo 'two words'`, "windows", true)
		if err != nil || !reflect.DeepEqual(args, []string{"/d", "/c", `echo "two words"`}) {
			t.Fatalf("prefix %q: args=%q err=%v", prefix, args, err)
		}
	}
	for _, prefix := range []string{"PS: ", "powershell: ", "pwsh: "} {
		_, args, err := buildCommandInvocation(prefix+`echo 'two words'`, "windows", true)
		if err != nil || args[len(args)-1] != `echo 'two words'` {
			t.Fatalf("prefix %q: args=%q err=%v", prefix, args, err)
		}
	}
	_, args, err := buildCommandInvocation(`echo 'two words'`, "linux", true)
	if err != nil || !reflect.DeepEqual(args, []string{"-c", `echo 'two words'`}) {
		t.Fatalf("non-Windows args=%q err=%v", args, err)
	}
}
