// Seam: kb-go's commands through cli.Run, as the Python `kb` hands them over, with a stand-in `kb` (docs/design.md §6).
package cli_test

import (
	"bytes"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/irodion/agent-fabric/go/internal/cli"
	"github.com/irodion/agent-fabric/go/internal/kbmcp"
)

// workspace is a stand-in `kb` whose `kb paths` names a wiki in its own folder, with one page.
func workspace(t *testing.T) kbmcp.Context {
	t.Helper()
	folder := t.TempDir()
	kb := filepath.Join(folder, "kb")
	if err := os.WriteFile(kb, []byte("#!/bin/sh\necho '{\"pages\": [\""+folder+"\"], \"index\": \""+folder+"/index.db\"}'\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(folder, "wiki", "projects"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(folder, "wiki", "projects", "a.md"), []byte("# A\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return kbmcp.Context{KB: kb, Cwd: folder}
}

func TestKbShowPrintsThePageGetPageGives(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := cli.Run([]string{"show", "wiki/projects/a.md"}, workspace(t), &stdout, &stderr)
	if code != 0 || !strings.Contains(stdout.String(), "\n# A\n</kb-page-") || stderr.Len() > 0 {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

func TestKbShowOfNoPageSaysSo(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := cli.Run([]string{"show", "wiki/projects/none.md"}, workspace(t), &stdout, &stderr)
	if code != 1 || stdout.Len() > 0 || stderr.String() != "kb show: no page wiki/projects/none.md\n" {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

func TestKbShowWithoutOnePathIsAUsageError(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := cli.Run([]string{"show"}, kbmcp.Context{}, &stdout, &stderr); code != 2 || stderr.Len() == 0 {
		t.Fatalf("exit %d, stderr %q", code, stderr.String())
	}
}

func TestKbShowHelpIsItsUsageNotAPage(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := cli.Run([]string{"show", "--help"}, kbmcp.Context{}, &stdout, &stderr)
	if code != 0 || !strings.HasPrefix(stdout.String(), "usage: ") || stderr.Len() > 0 {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

func TestKbShowWhereKbPathsFailsGivesItsWordsOnce(t *testing.T) {
	kb := filepath.Join(t.TempDir(), "kb")
	if err := os.WriteFile(kb, []byte("#!/bin/sh\necho 'kb: config.toml cannot be read' >&2\nexit 1\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	code := cli.Run([]string{"show", "wiki/projects/a.md"}, kbmcp.Context{KB: kb, Cwd: t.TempDir()}, &stdout, &stderr)
	if got := stderr.String(); code != 1 || got != "kb show: config.toml cannot be read\n" {
		t.Fatalf("exit %d, stderr %q", code, got)
	}
}

func TestKbSearchPrintsWhatSearchFindsWithItsOptionsAnywhere(t *testing.T) {
	ctx := workspace(t)
	var stdout, stderr bytes.Buffer
	code := cli.Run([]string{"search", "--kind", "page", "A", "--since=2000-01-01"}, ctx, &stdout, &stderr)
	if code != 0 || !strings.Contains(stdout.String(), "\nwiki/projects/a.md\n  # «A»\n</kb-search-") || stderr.Len() > 0 {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

func TestKbSearchThatFindsNothingPrintsNothing(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := cli.Run([]string{"search", "absent"}, workspace(t), &stdout, &stderr)
	if code != 0 || stdout.Len() > 0 || stderr.Len() > 0 {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

func TestKbSearchWithAnOptionItDoesntTakeIsAUsageError(t *testing.T) {
	ctx := workspace(t)
	for _, args := range [][]string{
		{"search", "--colour", "x"}, {"search", "x", "--kind"}, {"search"}, {"search", " "}, {"search", "x", "--project="},
		{"search", "x", "--kind", "claims"}, {"search", "x", "--since", "last week"},
	} {
		var stderr bytes.Buffer
		if code := cli.Run(args, ctx, io.Discard, &stderr); code != 2 || stderr.Len() == 0 {
			t.Errorf("%v: exit %d, stderr %q", args, code, stderr.String())
		}
	}
}

func TestKbReindexBuildsTheIndexAgainFromThePages(t *testing.T) {
	ctx := workspace(t)
	index := filepath.Join(ctx.Cwd, "index.db")
	if err := os.WriteFile(index, []byte("not an index"), 0o644); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	if code := cli.Run([]string{"reindex"}, ctx, &stdout, &stderr); code != 0 || stdout.String() != "kb reindex: 1 page in "+index+"\n" {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
	stdout.Reset()
	if code := cli.Run([]string{"search", "A"}, ctx, &stdout, &stderr); code != 0 || !strings.Contains(stdout.String(), "wiki/projects/a.md") {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

func TestHelpForACommandKbGoDoesntServeIsAUsageError(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if code := cli.Run([]string{"reindx", "--help"}, kbmcp.Context{}, &stdout, &stderr); code != 2 {
		t.Fatalf("exit %d, stdout %q", code, stdout.String())
	}
}

func TestKbSearchHelpAfterItsWordsIsItsUsage(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := cli.Run([]string{"search", "flaky", "--help"}, kbmcp.Context{}, &stdout, &stderr)
	if code != 0 || !strings.HasPrefix(stdout.String(), "usage: ") || stderr.Len() > 0 {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

func TestKbsOwnRefusalIsPrintedWithOnePrefix(t *testing.T) {
	kb := filepath.Join(t.TempDir(), "kb")
	if err := os.WriteFile(kb, []byte("#!/bin/sh\necho 'kb: that folder is in another workspace' >&2\nexit 2\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	cli.Run([]string{"search", "x", "--project", "../o"}, kbmcp.Context{KB: kb, Cwd: t.TempDir()}, io.Discard, &stderr)
	if got := stderr.String(); got != "kb search: that folder is in another workspace\n" {
		t.Fatalf("got %q", got)
	}
}

func TestKbReindexWhereTheIndexCantBeWrittenSaysWhereToRunIt(t *testing.T) {
	ctx := workspace(t)
	if code := cli.Run([]string{"reindex"}, ctx, io.Discard, io.Discard); code != 0 {
		t.Fatalf("exit %d", code)
	}
	if err := os.Chmod(filepath.Join(ctx.Cwd, "index.db"), 0o444); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(ctx.Cwd, 0o555); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(ctx.Cwd, 0o755) })
	var stderr bytes.Buffer
	if code := cli.Run([]string{"reindex"}, ctx, io.Discard, &stderr); code != 1 || !strings.Contains(stderr.String(), "can't be written here") {
		t.Fatalf("exit %d, stderr %q", code, stderr.String())
	}
}
