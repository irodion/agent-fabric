// Seam: kb-go's commands through cli.Run, as the Python `kb` hands them over, with a stand-in `kb` (docs/design.md §6).
package cli_test

import (
	"bytes"
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
	if err := os.WriteFile(kb, []byte("#!/bin/sh\necho '{\"pages\": [\""+folder+"\"]}'\n"), 0o755); err != nil {
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
	if got := stderr.String(); code != 1 || !strings.HasSuffix(got, "kb: config.toml cannot be read\n") {
		t.Fatalf("exit %d, stderr %q", code, got)
	}
}
