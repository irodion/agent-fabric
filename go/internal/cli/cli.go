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
var commands = []string{"mcp", "show", "search", "sessions", "catchup", "reindex"}

const usage = `usage: kb mcp
       kb show <path>    a page's path in the workspace, as wiki/projects/... or raw/sessions/...
       kb search <words> [--kind session|page|note] [--project ID] [--since DAY|AGE] [-- <words>]
       kb sessions [--project ID|FOLDER | --all-projects] [--file PATH] [--failing-tests] [--since DAY|AGE]
       kb catchup [--project ID|FOLDER]
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
	case len(args) >= 1 && args[0] == "sessions":
		var sessions kbmcp.SessionsArgs
		if code, done := wordless(args, stdout, stderr,
			map[string]*string{"--project": &sessions.Project, "--file": &sessions.File, "--since": &sessions.Since},
			map[string]*bool{"--all-projects": &sessions.AllProjects, "--failing-tests": &sessions.FailingTests}); done {
			return code
		}
		if err := ctx.CheckSessions(sessions); err != nil { // as for search: the command line's fault
			fmt.Fprintf(stderr, "kb sessions: %v\n%s\n", err, usage)
			return 2
		}
		out, err = ctx.Sessions(call, sessions)
	case len(args) >= 1 && args[0] == "catchup":
		var catchup kbmcp.CatchupArgs
		if code, done := wordless(args, stdout, stderr, map[string]*string{"--project": &catchup.Project}, nil); done {
			return code
		}
		out, err = ctx.Catchup(call, catchup)
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

// wordless reads the options of a command that takes no words, and is done, with the exit code, where it was asked
// for its usage or its command line is one it doesn't take.
func wordless(args []string, stdout, stderr io.Writer, values map[string]*string, flags map[string]*bool) (int, bool) {
	words, help, err := options(args[1:], values, flags)
	if help {
		fmt.Fprintln(stdout, usage)
		return 0, true
	}
	if err == nil && len(words) > 0 {
		err = fmt.Errorf("takes no words: %q", strings.Join(words, " "))
	}
	if err != nil {
		fmt.Fprintf(stderr, "kb %s: %v\n%s\n", args[0], err, usage)
		return 2, true
	}
	return 0, false
}

// searchArgs reads `kb search`'s words and options: --kind, --project and --since.
func searchArgs(args []string) (search kbmcp.SearchArgs, help bool, err error) {
	words, help, err := options(args,
		map[string]*string{"--kind": &search.Kind, "--project": &search.Project, "--since": &search.Since}, nil)
	if help || err != nil {
		return search, help, err
	}
	search.Query = strings.Join(words, " ")
	if strings.TrimSpace(search.Query) == "" { // as kb search "", which holds no word either
		return search, false, errors.New("no words to look for")
	}
	return search, false, nil
}

// options reads a command's words and options, in any order: each of `values` with its value as the next argument
// or after `=`, each of `flags` alone, and -h or --help, which asks for the usage; everything after `--` is words.
func options(args []string, values map[string]*string, flags map[string]*bool) (words []string, help bool, err error) {
	for i := 0; i < len(args); i++ {
		arg := args[i]
		if arg == "--" {
			return append(words, args[i+1:]...), false, nil
		}
		if !strings.HasPrefix(arg, "-") {
			words = append(words, arg)
			continue
		}
		if arg == "-h" || arg == "--help" {
			return nil, true, nil
		}
		if flag, known := flags[arg]; known {
			*flag = true
			continue
		}
		name, value, joined := strings.Cut(arg, "=")
		if _, flag := flags[name]; flag {
			return nil, false, fmt.Errorf("%s takes no value", name)
		}
		target, known := values[name]
		if !known {
			return nil, false, fmt.Errorf("no option %s; put a word that starts with - after --", name)
		}
		if !joined {
			if i+1 == len(args) || strings.HasPrefix(args[i+1], "-") { // --project=-x for one that starts with -
				return nil, false, fmt.Errorf("%s needs a value", name)
			}
			i++
			value = args[i]
		}
		if value == "" { // which the tool takes for no option, wider than asked for
			return nil, false, fmt.Errorf("%s needs a value", name)
		}
		*target = value
	}
	return words, false, nil
}
