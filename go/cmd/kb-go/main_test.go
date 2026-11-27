// Seam: `kb-go mcp` over real stdio, as an agent starts it, for the framing the in-memory tests don't cover.
package main

import (
	"context"
	"os"
	"os/exec"
	"slices"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// TestMain runs this test binary as kb-go itself when asked to, so the test needs no build of its own.
func TestMain(m *testing.M) {
	if os.Getenv("KB_GO_TEST_AS_MAIN") == "1" {
		main()
		return
	}
	os.Exit(m.Run())
}

func TestKbMcpServesItsToolsOverStdio(t *testing.T) {
	command := exec.Command(os.Args[0], "mcp")
	command.Env = append(os.Environ(), "KB_GO_TEST_AS_MAIN=1")
	ctx := context.Background()
	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	session, err := client.Connect(ctx, &mcp.CommandTransport{Command: command}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	tools, err := session.ListTools(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, tool := range tools.Tools {
		names = append(names, tool.Name)
	}
	if !slices.Contains(names, "list_notes") {
		t.Fatalf("got %v", names)
	}
}
