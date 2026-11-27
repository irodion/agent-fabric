// Package cli is kb-go's entry: the commands written in Go, which the Python `kb` hands over to (docs/design.md §6).
package cli

import (
	"context"
	"fmt"
	"io"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/irodion/agent-fabric/go/internal/kbmcp"
)

const usage = "usage: kb mcp | kb show <path, as wiki/projects/... or raw/sessions/...>"

// Run runs one command and gives its exit code: 1 where it fails, 2 for a command line kb-go doesn't take. `kb mcp`
// serves on the process's stdin and stdout; the other commands print what the MCP tool of the same job answers.
func Run(args []string, ctx kbmcp.Context, stdout, stderr io.Writer) int {
	switch {
	case len(args) == 1 && args[0] == "mcp":
		if err := kbmcp.NewServer(ctx).Run(context.Background(), &mcp.StdioTransport{}); err != nil {
			fmt.Fprintf(stderr, "kb mcp: %v\n", err)
			return 1
		}
		return 0
	case len(args) == 2 && args[0] == "show" && (args[1] == "-h" || args[1] == "--help"):
		fmt.Fprintln(stdout, usage)
		return 0
	case len(args) == 2 && args[0] == "show" && !strings.HasPrefix(args[1], "-"):
		page, err := ctx.Page(context.Background(), args[1])
		if err != nil {
			// kb's own words end in a newline already.
			fmt.Fprintf(stderr, "kb show: %s\n", strings.TrimRight(err.Error(), "\n"))
			return 1
		}
		fmt.Fprint(stdout, page)
		return 0
	}
	fmt.Fprintln(stderr, usage)
	return 2
}
