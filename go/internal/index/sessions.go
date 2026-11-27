package index

import (
	"bytes"
	"database/sql"

	"go.yaml.in/yaml/v3"
)

// sessionsSchema is a row per session digest and one per file it changed, from its front matter (docs/design.md
// §4.4), for list_sessions: digest.py writes it, and kb-go is its one reader.
const sessionsSchema = `
CREATE TABLE sessions (
	page INTEGER PRIMARY KEY, agent TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
	tests TEXT NOT NULL, -- the last test run's: passed, failed, or "" for none
	prompts INTEGER NOT NULL, errors INTEGER NOT NULL
);
CREATE TABLE files (page INTEGER NOT NULL, path TEXT NOT NULL, PRIMARY KEY (page, path)) WITHOUT ROWID;
CREATE INDEX files_by_path ON files (path, page);
`

// front is what list_sessions reads of a digest's front matter.
type front struct {
	Schema string `yaml:"schema"`
	Agent  string `yaml:"agent"`
	Time   struct {
		Start string `yaml:"start"`
		End   string `yaml:"end"`
	} `yaml:"time"`
	Outcome struct {
		Tests string `yaml:"last_tests"` // "" for none, as YAML's null
	} `yaml:"outcome"`
	Counts struct {
		Prompts int `yaml:"prompts"`
		Errors  int `yaml:"errors"`
	} `yaml:"counts"`
	Files []string `yaml:"files_modified"`
}

// session adds a digest's row and its files: nothing for a page whose front matter isn't a digest's, which still
// counts for search.
func session(tx *sql.Tx, id int64, body []byte) error {
	head, ok := bytes.CutPrefix(body, []byte("---\n"))
	if !ok {
		return nil
	}
	head, _, ok = bytes.Cut(head, []byte("\n---\n"))
	var digest front
	if !ok || yaml.Unmarshal(head, &digest) != nil || digest.Schema != "fabric.digest/1" || digest.Time.Start == "" {
		return nil
	}
	if _, err := tx.Exec(`INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)`, id, digest.Agent, digest.Time.Start,
		digest.Time.End, digest.Outcome.Tests, digest.Counts.Prompts, digest.Counts.Errors); err != nil {
		return err
	}
	for _, file := range digest.Files {
		// OR IGNORE: a file named twice, as a digest edited by hand may, is no reason to fail every search.
		if _, err := tx.Exec(`INSERT OR IGNORE INTO files VALUES (?, ?)`, id, file); err != nil {
			return err
		}
	}
	return nil
}

// Session is a session as its digest's front matter says: where the digest is, which agent, when, how it went.
type Session struct {
	Path, Agent, Start, End string
	Tests                   string // "" where it ran none
	Prompts, Errors         int
}

// SessionQuery is which sessions to list: those whose digest is filed by month in one of Months, that changed File
// where given, whose last tests failed where FailingTests, started on or after Since where given (yyyy-mm-dd); at
// most Limit, the newest.
type SessionQuery struct {
	Months       []string
	File         string
	FailingTests bool
	Since        string
	Limit        int
}

// Sessions brings the index at `file` up to date, as Search does, then lists the sessions the query asks for,
// newest first.
func Sessions(file string, folders []string, query SessionQuery) ([]Session, error) {
	db, err := reader(file, folders)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	// Since by the day the digest's name starts with, as search has it: its start's, in UTC.
	where, args := Query{Months: query.Months, Since: query.Since}.where()
	if query.File != "" {
		where, args = where+" AND s.page IN (SELECT page FROM files WHERE path = ?)", append(args, query.File)
	}
	if query.FailingTests {
		where += " AND s.tests = 'failed'"
	}
	rows, err := db.Query(`SELECT p.path, s.agent, s.start, s.end, s.tests, s.prompts, s.errors
		FROM sessions s JOIN pages p ON p.id = s.page WHERE `+where+` ORDER BY s.start DESC, p.path DESC LIMIT ?`,
		append(args, query.Limit)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var found []Session
	for rows.Next() {
		var s Session
		if err := rows.Scan(&s.Path, &s.Agent, &s.Start, &s.End, &s.Tests, &s.Prompts, &s.Errors); err != nil {
			return nil, err
		}
		found = append(found, s)
	}
	return found, rows.Err()
}
