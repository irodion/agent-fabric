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

// fakeKB is a stand-in `kb` in a folder of its own, a shell script with this body.
func fakeKB(t *testing.T, body string) string {
	t.Helper()
	kb := filepath.Join(t.TempDir(), "kb")
	if err := os.WriteFile(kb, []byte("#!/bin/sh\n"+body), 0o755); err != nil {
		t.Fatal(err)
	}
	return kb
}

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
	kb := fakeKB(t, "echo 'kb: config.toml cannot be read' >&2\nexit 1\n")
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
	kb := fakeKB(t, "echo 'kb: that folder is in another workspace' >&2\nexit 2\n")
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

func TestKbSessionsPrintsWhatListSessionsGivesWithItsOptions(t *testing.T) {
	ctx := workspace(t)
	digest := "---\nschema: \"fabric.digest/1\"\nagent: \"codex\"\ntime: {start: \"2026-11-26T14:00:00Z\", end: " +
		"\"2026-11-26T14:30:00Z\", active_min: 3}\noutcome: {last_tests: \"failed\"}\ncounts: {prompts: 1, errors: 0}\n" +
		"files_modified: [\"src/app.py\"]\n---\n"
	file := filepath.Join(ctx.Cwd, "raw", "sessions", "p", "2026", "11", "2026-11-26-codex-aaaa.md")
	if err := os.MkdirAll(filepath.Dir(file), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(file, []byte(digest), 0o644); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	code := cli.Run([]string{"sessions", "--all-projects", "--failing-tests", "--file", "src/app.py"}, ctx, &stdout, &stderr)
	want := "\nraw/sessions/p/2026/11/2026-11-26-codex-aaaa.md\n  codex · 2026-11-26 14:00–14:30 · 1 prompt · 0 errors · tests failed\n"
	if code != 0 || !strings.Contains(stdout.String(), want) || stderr.Len() > 0 {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

func TestKbSessionsTakesNoWords(t *testing.T) {
	var stderr bytes.Buffer
	if code := cli.Run([]string{"sessions", "flaky"}, workspace(t), io.Discard, &stderr); code != 2 || stderr.Len() == 0 {
		t.Fatalf("exit %d, stderr %q", code, stderr.String())
	}
}

func TestKbSessionsSaysAFlagTakesNoValue(t *testing.T) {
	var stderr bytes.Buffer
	code := cli.Run([]string{"sessions", "--failing-tests=false"}, workspace(t), io.Discard, &stderr)
	if code != 2 || !strings.Contains(stderr.String(), "--failing-tests takes no value") {
		t.Fatalf("exit %d, stderr %q", code, stderr.String())
	}
}

func TestKbCatchupPrintsWhatCatchupGives(t *testing.T) {
	ctx := workspace(t)
	kb := filepath.Join(ctx.Cwd, "kb")
	paths := `{"pages": ["` + ctx.Cwd + `"], "index": "` + ctx.Cwd + `/index.db", "project": {"session": ` +
		`"raw/sessions/p/", "note": "raw/notes/p/", "page": "wiki/projects/a.md"}}`
	if err := os.WriteFile(kb, []byte("#!/bin/sh\n[ \"$1\" = paths ] && echo '"+paths+"'\nexit 0\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	code := cli.Run([]string{"catchup", "--project", "p"}, ctx, &stdout, &stderr)
	if code != 0 || !strings.Contains(stdout.String(), "## Project page: wiki/projects/a.md") || stderr.Len() > 0 {
		t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout.String(), stderr.String())
	}
}

func TestKbSessionsWithASinceOrProjectsItCantTakeIsAUsageError(t *testing.T) {
	for _, args := range [][]string{
		{"sessions", "--since", "yesterday"}, {"sessions", "--project", "p", "--all-projects"},
	} {
		var stderr bytes.Buffer
		if code := cli.Run(args, workspace(t), io.Discard, &stderr); code != 2 || !strings.Contains(stderr.String(), "usage: ") {
			t.Errorf("%v: exit %d, stderr %q", args, code, stderr.String())
		}
	}
}

func TestAnOptionGivenNoValueTakesNotTheNextOptionForOne(t *testing.T) {
	var stderr bytes.Buffer
	code := cli.Run([]string{"sessions", "--project", "--failing-tests"}, workspace(t), io.Discard, &stderr)
	if code != 2 || !strings.Contains(stderr.String(), "--project needs a value") {
		t.Fatalf("exit %d, stderr %q", code, stderr.String())
	}
}
