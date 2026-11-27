// Package cli is kb-go's entry: the commands written in Go, which the Python `kb` hands over to (docs/design.md §6).
package cli

import (
	"context"
	"errors"
	"fmt"
	"io"
	"slices"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/irodion/agent-fabric/go/internal/kbmcp"
)

// commands are the commands kb-go serves, as the Python kb's GO_COMMANDS names them.
var commands = []string{"mcp", "show", "search", "reindex"}

const usage = `usage: kb mcp
       kb show <path>    a page's path in the workspace, as wiki/projects/... or raw/sessions/...
       kb search <words> [--kind session|page|note] [--project ID] [--since DAY|AGE] [-- <words>]
       kb reindex`

// Run runs one command and gives its exit code: 1 where it fails, 2 for a command line kb-go doesn't take. `kb mcp`
// serves on the process's stdin and stdout; the other commands print what the MCP tool of the same job answers.
func Run(args []string, ctx kbmcp.Context, stdout, stderr io.Writer) int {
	if len(args) == 2 && slices.Contains(commands, args[0]) && (args[1] == "-h" || args[1] == "--help") {
		fmt.Fprintln(stdout, usage)
		return 0
	}
	call := context.Background()
	var out string
	var err error
	switch {
	case len(args) == 1 && args[0] == "mcp":
		if err := kbmcp.NewServer(ctx).Run(call, &mcp.StdioTransport{}); err != nil {
			fmt.Fprintf(stderr, "kb mcp: %v\n", err)
			return 1
		}
		return 0
	case len(args) == 2 && args[0] == "show" && !strings.HasPrefix(args[1], "-"):
		out, err = ctx.Page(call, args[1])
	case len(args) >= 1 && args[0] == "search":
		search, help, usageErr := searchArgs(args[1:])
		if help {
			fmt.Fprintln(stdout, usage)
			return 0
		}
		if usageErr == nil {
			usageErr = ctx.Check(search) // a kind or a since it doesn't know is the command line's fault too
		}
		if usageErr != nil {
			fmt.Fprintf(stderr, "kb search: %v\n%s\n", usageErr, usage)
			return 2
		}
		out, err = ctx.Search(call, search)
	case len(args) == 1 && args[0] == "reindex":
		out, err = ctx.Reindex(call)
		if err == nil {
			out = "kb reindex: " + out + "\n"
		}
	default:
		fmt.Fprintln(stderr, usage)
		return 2
	}
	if err != nil {
		fmt.Fprintf(stderr, "kb %s: %s\n", args[0], strings.TrimPrefix(strings.TrimSuffix(err.Error(), "\n"), "kb: "))
		return 1
	}
	fmt.Fprint(stdout, out)
	return 0
}

// searchArgs reads `kb search`'s words and options, in any order: --kind, --project and --since, each with its
// value as the next argument or after `=`, and -h or --help, which asks for the usage; everything after `--` is words.
func searchArgs(args []string) (search kbmcp.SearchArgs, help bool, err error) {
	options := map[string]*string{"--kind": &search.Kind, "--project": &search.Project, "--since": &search.Since}
	var words []string
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if arg == "--" {
			words = append(words, args[i+1:]...)
			break
		}
		if !strings.HasPrefix(arg, "-") {
			words = append(words, arg)
			continue
		}
		if arg == "-h" || arg == "--help" {
			return search, true, nil
		}
		name, value, joined := strings.Cut(arg, "=")
		target, known := options[name]
		if !known {
			return search, false, fmt.Errorf("no option %s; put a word that starts with - after --", name)
		}
		if !joined {
			if i+1 == len(args) {
				return search, false, fmt.Errorf("%s needs a value", name)
			}
			i++
			value = args[i]
		}
		if value == "" { // which the tool takes for no option, a search wider than asked for
			return search, false, fmt.Errorf("%s needs a value", name)
		}
		*target = value
	}
	search.Query = strings.Join(words, " ")
	if strings.TrimSpace(search.Query) == "" { // as kb search "", which holds no word either
		return search, false, errors.New("no words to look for")
	}
	return search, false, nil
}
