// Seam: `kb mcp`'s tools, called by name through an MCP client over the SDK's in-memory transport (docs/design.md
// §4.9). Until the port brings `kb inbox` over (§6), list_notes runs it: a stand-in `kb` records what it was asked.
package kbmcp_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/irodion/agent-fabric/go/internal/kbmcp"
)

// stub writes a stand-in `kb` that prints the folder it ran in and its arguments, one per line, then `script`.
func stub(t *testing.T, script string) string {
	t.Helper()
	kb := filepath.Join(t.TempDir(), "kb")
	body := "#!/bin/sh\npwd\nfor arg in \"$@\"; do echo \"[$arg]\"; done\n" + script
	if err := os.WriteFile(kb, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	return kb
}

// call runs one tool through a client connected to kb's server.
func call(t *testing.T, ctx kbmcp.Context, tool string, args map[string]any) *mcp.CallToolResult {
	t.Helper()
	background := context.Background()
	serverSide, clientSide := mcp.NewInMemoryTransports()
	if _, err := kbmcp.NewServer(ctx).Connect(background, serverSide, nil); err != nil {
		t.Fatal(err)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil)
	session, err := client.Connect(background, clientSide, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = session.Close() })
	result, err := session.CallTool(background, &mcp.CallToolParams{Name: tool, Arguments: args})
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func text(t *testing.T, result *mcp.CallToolResult) string {
	t.Helper()
	if len(result.Content) != 1 {
		t.Fatalf("%+v", result)
	}
	return result.Content[0].(*mcp.TextContent).Text
}

func TestListNotesIsKbInboxInTheServersFolder(t *testing.T) {
	folder := t.TempDir()
	result := call(t, kbmcp.Context{KB: stub(t, ""), Cwd: folder}, "list_notes", nil)
	if got, want := text(t, result), folder+"\n[inbox]\n"; result.IsError || got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}

func TestListNotesPassesEachArgumentAsOneValueKbInboxCantReadAsAnOption(t *testing.T) {
	folder := t.TempDir()
	args := map[string]any{"project": "--all-projects", "since": "7d", "kind": "status", "all": true}
	result := call(t, kbmcp.Context{KB: stub(t, ""), Cwd: folder}, "list_notes", args)
	want := folder + "\n[inbox]\n[--project=--all-projects]\n[--since=7d]\n[--kind=status]\n[--all]\n"
	if got := text(t, result); result.IsError || got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}

func TestListNotesThatKbInboxRefusesIsAToolErrorWithItsReason(t *testing.T) {
	kb := stub(t, "echo 'kb: --since 7x is not an age like 7d or 24h' >&2\nexit 2\n")
	result := call(t, kbmcp.Context{KB: kb, Cwd: t.TempDir()}, "list_notes", map[string]any{"since": "7x"})
	if got := text(t, result); !result.IsError || got != "kb: --since 7x is not an age like 7d or 24h\n" {
		t.Fatalf("got %q (error: %v)", got, result.IsError)
	}
}
