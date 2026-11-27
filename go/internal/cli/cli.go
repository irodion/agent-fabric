// Package cli is kb-go's entry: the commands written in Go, which the Python `kb` hands over to (docs/design.md §6).
package cli

import (
	"context"
	"fmt"
	"io"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/irodion/agent-fabric/go/internal/kbmcp"
)

// Run runs one command and gives its exit code: 2 for a command line kb-go doesn't take. `kb mcp` serves on the
// process's stdin and stdout.
func Run(args []string, ctx kbmcp.Context, stderr io.Writer) int {
	if len(args) != 1 || args[0] != "mcp" {
		fmt.Fprintln(stderr, "usage: kb-go mcp")
		return 2
	}
	if err := kbmcp.NewServer(ctx).Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		fmt.Fprintf(stderr, "kb mcp: %v\n", err)
		return 1
	}
	return 0
}
