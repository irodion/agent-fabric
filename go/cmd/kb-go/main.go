// kb-go is the part of kb written in Go (docs/design.md §6); the Python `kb` hands it the commands it serves.
package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/irodion/agent-fabric/go/internal/cli"
	"github.com/irodion/agent-fabric/go/internal/kbmcp"
)

func main() {
	cwd, err := os.Getwd()
	if err != nil {
		fmt.Fprintf(os.Stderr, "kb: the current folder can't be read: %v\n", err)
		os.Exit(1)
	}
	self, err := os.Executable()
	if err != nil {
		fmt.Fprintf(os.Stderr, "kb: kb-go can't find itself: %v\n", err)
		os.Exit(1)
	}
	// The kb that scripts/install.sh put beside kb-go: the one that handed over to it. A kb-go without one, as kb
	// finds on PATH when none is beside it, asks the kb on PATH.
	kb := filepath.Join(filepath.Dir(self), "kb")
	if _, err := os.Stat(kb); err != nil {
		if onPath, err := exec.LookPath("kb"); err == nil {
			kb = onPath
		}
	}
	os.Exit(cli.Run(os.Args[1:], kbmcp.Context{KB: kb, Cwd: cwd}, os.Stderr))
}
