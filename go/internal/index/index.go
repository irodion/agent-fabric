// Package index is a workspace's search index, index.db: SQLite's FTS5 over its pages, which it can rebuild from
// them whenever it's lost (docs/design.md §4.9).
package index

import (
	"database/sql"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"modernc.org/sqlite"

	"github.com/irodion/agent-fabric/go/internal/pages"
)

// version is the index's schema: an index of another version is built again, as it can always be. 2 added the
// sessions and files tables.
const version = 2

// schema is the index: a row per page, and its text in FTS5 under the same rowid, so that a page is dropped by its
// rowid; FTS5 finds a row by any other column only by reading every one.
const schema = `
CREATE TABLE pages (
	id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, size INTEGER NOT NULL, mtime INTEGER NOT NULL,
	day TEXT NOT NULL -- yyyy-mm-dd: the date its name starts with, else the day it was last written (UTC)
);
CREATE VIRTUAL TABLE text USING fts5(body, tokenize = 'porter unicode61');
`

// Query is what to look for: every word, in the pages whose path starts with one of Prefixes, is filed by month in
// one of Months, or is one of Paths, where any is given, dated on or after Since where given (yyyy-mm-dd); at most
// Limit pages, the best.
type Query struct {
	Words    string
	Prefixes []string
	Months   []string // folders whose pages are in yyyy/mm/ right below them, as a project's digests and notes are
	Paths    []string
	Since    string
	Limit    int
}

// where is the query's narrowing, as SQL over pages p, and its arguments.
func (q Query) where() (string, []any) {
	var paths []string
	var args []any
	for _, prefix := range q.Prefixes {
		// substr counts characters, not bytes, as a project id outside ASCII has more of.
		paths, args = append(paths, "substr(p.path, 1, ?) = ?"), append(args, utf8.RuneCountInString(prefix), prefix)
	}
	for _, folder := range q.Months {
		// Only a page in the folder's own yyyy/mm/, not one in a folder below it: the project github.com/me's
		// raw/sessions/github.com/me/ holds github.com/me/app's folder too.
		n := utf8.RuneCountInString(folder)
		paths = append(paths, "(substr(p.path, 1, ?) = ? AND substr(p.path, ?) GLOB '[0-9][0-9][0-9][0-9]/[0-9][0-9]/*'"+
			" AND instr(substr(p.path, ?), '/') = 0)")
		args = append(args, n, folder, n+1, n+9)
	}
	for _, path := range q.Paths {
		paths, args = append(paths, "p.path = ?"), append(args, path)
	}
	where := "1"
	if len(paths) > 0 {
		where = "(" + strings.Join(paths, " OR ") + ")"
	}
	if q.Since != "" {
		where, args = where+" AND p.day >= ?", append(args, q.Since)
	}
	return where, args
}

// Hit is a page that holds what was searched for, with the words found in their context.
type Hit struct {
	Path    string
	Snippet string
}

// Search brings the index at `file` up to date with the page folders, in order, the first holding a path winning,
// then finds the pages that hold every word of the query, best first. An index of another version, or a file that
// isn't one, is built again. Where the index can't be written, as in an agent's sandbox, it's searched as it stands.
func Search(file string, folders []string, query Query) ([]Hit, error) {
	match := phrases(query.Words)
	if match == "" {
		return nil, errors.New("a search needs a word to look for")
	}
	db, err := reader(file, folders)
	if err != nil {
		return nil, err
	}
	defer db.Close()
	where, args := query.where()
	rows, err := db.Query(`SELECT p.path, snippet(text, 0, '«', '»', '…', 12) FROM text t JOIN pages p ON p.id = t.rowid
		WHERE text MATCH ? AND `+where+` ORDER BY t.rank LIMIT ?`, append(append([]any{match}, args...), query.Limit)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var hits []Hit
	for rows.Next() {
		var hit Hit
		if err := rows.Scan(&hit.Path, &hit.Snippet); err != nil {
			return nil, err
		}
		hit.Snippet = strings.Join(strings.Fields(hit.Snippet), " ")
		hits = append(hits, hit)
	}
	return hits, rows.Err()
}

// reader is the index at `file` up to date with the page folders, to read; where it can't be written, as in an
// agent's sandbox, or another is building it still, as it stands.
func reader(file string, folders []string) (*sql.DB, error) {
	db, err := current(file, folders)
	if readOnly(err) || code(err) == busy { // busy: another search still building it, which a reader can wait out
		db, err = openReadOnly(file)
		if err == nil {
			if err = filled(db, file, folders); err != nil {
				db.Close()
			}
		}
	}
	return db, err
}

// Rebuild builds the index at `file` again from the page folders, and gives how many pages it holds. An index of this
// version is emptied and filled again in one transaction, in place: a file moved over one that another search is
// writing would take that search's journal for its own and roll it back into the new file. Only a file of another
// version, or one that isn't an index, is replaced by one built in a new file of its own.
func Rebuild(file string, folders []string) (int, error) {
	count, err := rebuild(file, folders)
	if readOnly(err) {
		return 0, fmt.Errorf("the search index at %s can't be written here, as in an agent's sandbox: run kb reindex "+
			"where kb's data can be written (%w)", file, err)
	}
	return count, err
}

func rebuild(file string, folders []string) (int, error) {
	if err := os.MkdirAll(filepath.Dir(file), 0o700); err != nil {
		return 0, err
	}
	db, err := open(file)
	if errors.Is(err, errStale) {
		return replace(file, folders)
	}
	if err != nil {
		return 0, err
	}
	return fillAndClose(db, folders, true)
}

// replace builds the index in a new file of its own, so that two rebuilds at once don't write one file, then moves
// it over `file`, which is no index of this version, and drops any journal left beside that, which SQLite would
// otherwise roll back into the new file. One at a time, under a lock beside the index, which looks at the file again
// first: once another has moved its index in, a search may be writing to it, its journal beside it, and that index
// is brought up to date instead.
func replace(file string, folders []string) (int, error) {
	lock, err := os.OpenFile(file+".lock", os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return 0, err
	}
	defer lock.Close()                                      // and with it the lock
	if err := lockWithin(lock, 5*time.Second); err != nil { // as busy_timeout waits for SQLite's own lock
		return 0, err
	}
	if db, err := open(file); err == nil {
		return fillAndClose(db, folders, false)
	} else if !errors.Is(err, errStale) {
		return 0, err
	}
	made, err := os.CreateTemp(filepath.Dir(file), filepath.Base(file)+".*.new")
	if err != nil {
		return 0, err
	}
	fresh := made.Name()
	if err := made.Close(); err != nil {
		_ = os.Remove(fresh)
		return 0, err
	}
	db, err := open(fresh)
	count := 0
	if err == nil {
		count, err = fillAndClose(db, folders, false)
	}
	if err == nil {
		if err = os.Remove(file + "-journal"); errors.Is(err, fs.ErrNotExist) {
			err = nil
		}
	}
	if err == nil {
		err = os.Rename(fresh, file)
	}
	if err != nil {
		_ = os.Remove(fresh)
		return 0, err
	}
	return count, nil
}

// fill brings the index up to date with the page folders, emptied first where `again`, and gives how many pages it
// holds.
func fill(db *sql.DB, folders []string, again bool) (int, error) {
	if err := sync(db, folders, again); err != nil {
		return 0, err
	}
	count := 0
	err := db.QueryRow(`SELECT count(*) FROM pages`).Scan(&count)
	return count, err
}

// errBuilding is the lock replace takes, held longer than a search waits: by a kb reindex stopped mid-build, say.
var errBuilding = errors.New("another kb is building the search index again: try again in a moment")

// lockWithin takes `lock` within `wait`, or gives errBuilding.
func lockWithin(lock *os.File, wait time.Duration) error {
	for deadline := time.Now().Add(wait); ; time.Sleep(50 * time.Millisecond) {
		err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if !errors.Is(err, syscall.EWOULDBLOCK) {
			return err
		}
		if time.Now().After(deadline) {
			return errBuilding
		}
	}
}

// fillAndClose fills the index as fill does, then closes it, and gives fill's error before Close's.
func fillAndClose(db *sql.DB, folders []string, again bool) (int, error) {
	count, err := fill(db, folders, again)
	if closed := db.Close(); err == nil {
		err = closed
	}
	return count, err
}

// phrases is the query as FTS5 reads it: each word a phrase of its own, quoted, so that no word is read as FTS5's
// syntax, and a page has to hold all of them.
func phrases(query string) string {
	var quoted []string
	for _, word := range strings.Fields(query) {
		quoted = append(quoted, `"`+strings.ReplaceAll(word, `"`, `""`)+`"`)
	}
	return strings.Join(quoted, " ")
}

// errStale is an index of another version, or a file that isn't one.
var errStale = errors.New("not an index of this version")

// errNewer is an index a newer kb-go built, which this one can't read, and mustn't build again as its own: the newer
// one would build it back on its next search, and each again on every search after.
var errNewer = errors.New("a newer kb-go built this index: restart this kb mcp, which runs an older one, or run " +
	"scripts/install.sh again")

// current is the index at `file`, built again where it's stale, brought up to date with the page folders.
func current(file string, folders []string) (*sql.DB, error) {
	if err := os.MkdirAll(filepath.Dir(file), 0o700); err != nil {
		return nil, err
	}
	db, err := open(file)
	if errors.Is(err, errStale) {
		if _, err = replace(file, folders); err == nil {
			db, err = open(file)
		}
	}
	if err != nil {
		return nil, err
	}
	if err := sync(db, folders, false); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

// open opens the index at `file` to write, making it where there's none: errStale for one of another version, or a
// file that isn't one or is damaged. Its transactions take the write lock as they begin, so that two searches
// bringing it up to date at once wait for each other, where two that read first and then write would leave one
// SQLITE_BUSY at once.
func open(file string) (*sql.DB, error) {
	db, err := sql.Open("sqlite",
		"file:"+uri(file)+"?_txlock=immediate&_pragma=busy_timeout(5000)&_pragma=journal_mode(DELETE)")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if err = create(db); code(err) == notADatabase || code(err) == corrupt {
		err = errStale
	}
	if err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}

// uri is `file` as an SQLite URI's path, so that a ?, # or % in a folder's name is no query, fragment or escape.
var uri = strings.NewReplacer("%", "%25", "?", "%3f", "#", "%23").Replace

// openReadOnly opens an index that can't be written, as it stands: in journal_mode=DELETE, a reader makes no file
// beside it (docs/design.md §4.1).
func openReadOnly(file string) (*sql.DB, error) {
	fix := "run kb reindex where kb's data can be written, outside an agent's sandbox"
	if _, err := os.Stat(file); err != nil {
		return nil, fmt.Errorf("no search index at %s, and none can be made here: %s", file, fix)
	}
	db, err := sql.Open("sqlite", "file:"+uri(file)+"?mode=ro&_pragma=busy_timeout(5000)")
	if err != nil {
		return nil, err
	}
	if was, _, err := versionOf(db); err != nil || was != version {
		db.Close()
		return nil, fmt.Errorf("the search index at %s is of another kb, and can't be built again here: %s", file, fix)
	}
	return db, nil
}

// create makes the index's tables where the file has none, in one transaction, so that two searches finding no index
// make it once: errStale for an index of another version.
func create(db *sql.DB) error {
	if was, _, err := versionOf(db); err == nil && was == version {
		return nil // made already: no write lock to take, as there's nothing to write
	} else if err == nil && was > version {
		return errNewer // where it can't be written too, which would otherwise call it another kb's
	}
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	was, tables, err := versionOf(tx)
	switch {
	case err != nil:
		return err
	case was == 0 && tables == 0:
		if _, err := tx.Exec(schema + sessionsSchema + fmt.Sprintf("PRAGMA user_version = %d;", version)); err != nil {
			return err
		}
	case was > version:
		return errNewer
	case was != version:
		return errStale
	}
	return tx.Commit()
}

type querier interface {
	QueryRow(query string, args ...any) *sql.Row
}

// filled says an index read as it stands holds pages, or that the folders hold none for it to: one made and not
// filled yet, as another search's first build leaves it for a moment, would otherwise answer nothing, as if no page
// matched, while an empty workspace's index answers nothing because nothing is there.
func filled(db *sql.DB, file string, folders []string) error {
	var any int
	if err := db.QueryRow(`SELECT count(*) FROM (SELECT 1 FROM pages LIMIT 1)`).Scan(&any); err != nil {
		return err
	}
	if any == 0 && hasPages(folders) {
		return fmt.Errorf("the search index at %s holds no pages yet: another kb may be building it, so try again "+
			"in a moment, or run kb reindex where kb's data can be written", file)
	}
	return nil
}

// hasPages says whether the folders hold a page; one that can't be walked counts as holding some.
func hasPages(folders []string) bool {
	found := false
	err := pages.Walk(folders, func(string, string, fs.FileInfo) { found = true })
	return found || err != nil
}

func versionOf(db querier) (was, tables int, err error) {
	if err = db.QueryRow(`PRAGMA user_version`).Scan(&was); err == nil {
		err = db.QueryRow(`SELECT count(*) FROM sqlite_schema`).Scan(&tables)
	}
	return was, tables, err
}

// SQLite's primary result codes (sqlite3.h).
const (
	perm         = 3
	busy         = 5
	readonly     = 8
	corrupt      = 11
	cantOpen     = 14
	notADatabase = 26
)

func code(err error) int {
	var failed *sqlite.Error
	if errors.As(err, &failed) {
		return failed.Code() & 0xff
	}
	return 0
}

// readOnly says whether an error is the index, or its folder, refusing a write: a read-only mount, a permission, or
// a sandbox's rule, which only a write finds where access(2) may say yes.
func readOnly(err error) bool {
	switch code(err) {
	case perm, readonly, cantOpen:
		return true
	}
	return errors.Is(err, fs.ErrPermission) || errors.Is(err, syscall.EROFS)
}

type stamp struct {
	folder      string
	size, mtime int64
	day         string
}

var dated = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}`)

type indexedPage struct {
	id          int64
	path        string
	size, mtime int64
}

// indexedPages are the pages the index holds, as they were when indexed.
func indexedPages(db interface {
	Query(query string, args ...any) (*sql.Rows, error)
}) ([]indexedPage, error) {
	rows, err := db.Query(`SELECT id, path, size, mtime FROM pages`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var indexed []indexedPage
	for rows.Next() {
		var page indexedPage
		if err := rows.Scan(&page.id, &page.path, &page.size, &page.mtime); err != nil {
			return nil, err
		}
		indexed = append(indexed, page)
	}
	return indexed, rows.Err()
}

// unchanged says whether the pages found are the pages indexed, each of the size and time it was indexed at.
func unchanged(indexed []indexedPage, found map[string]stamp) bool {
	if len(indexed) != len(found) {
		return false
	}
	for _, page := range indexed {
		if now, ok := found[page.path]; !ok || now.size != page.size || now.mtime != page.mtime {
			return false
		}
	}
	return true
}

// sync re-reads each page whose size or time changed since it was indexed, and drops each that's gone; where `again`,
// it drops every page first, and reads them all.
func sync(db *sql.DB, folders []string, again bool) error {
	found := map[string]stamp{}
	err := pages.Walk(folders, func(rel, folder string, info fs.FileInfo) {
		day := dated.FindString(filepath.Base(rel))
		if day == "" {
			day = info.ModTime().UTC().Format(time.DateOnly)
		}
		found[rel] = stamp{folder, info.Size(), info.ModTime().UnixNano(), day}
	})
	if err != nil {
		// %v, not %w: a page folder that can't be read is no index that can't be written, which readOnly takes for
		// a reason to search the index as it stands.
		return fmt.Errorf("the pages can't be read: %v", err)
	}
	if !again {
		indexed, err := indexedPages(db)
		if err != nil {
			return err
		}
		if unchanged(indexed, found) {
			return nil // nothing to write, so no write lock to wait for, or to be refused, as in an agent's sandbox
		}
	}
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if again {
		if _, err := tx.Exec(`DELETE FROM text; DELETE FROM pages; DELETE FROM sessions; DELETE FROM files;`); err != nil {
			return err
		}
	}
	indexed, err := indexedPages(tx) // again, under the write lock: another search may have changed it since
	if err != nil {
		return err
	}
	for _, r := range indexed {
		if now, ok := found[r.path]; ok && now.size == r.size && now.mtime == r.mtime {
			delete(found, r.path) // as indexed
			continue
		}
		for _, table := range []string{"text WHERE rowid", "pages WHERE id", "sessions WHERE page", "files WHERE page"} {
			if _, err := tx.Exec(`DELETE FROM `+table+` = ?`, r.id); err != nil {
				return err
			}
		}
	}
	for path, now := range found {
		body, err := pages.Read(now.folder, path)
		if errors.Is(err, fs.ErrNotExist) || errors.Is(err, fs.ErrPermission) {
			continue // gone since the walk, or not ours to read: left out, as the walk leaves out a folder it can't read
		}
		if err != nil {
			// %v, as for the walk: a page that can't be read is no index that can't be written.
			return fmt.Errorf("page %s can't be read: %v", path, err)
		}
		added, err := tx.Exec(`INSERT INTO pages (path, size, mtime, day) VALUES (?, ?, ?, ?)`,
			path, now.size, now.mtime, now.day)
		if err != nil {
			return err
		}
		id, err := added.LastInsertId()
		if err != nil {
			return err
		}
		if _, err := tx.Exec(`INSERT INTO text (rowid, body) VALUES (?, ?)`, id, string(body)); err != nil {
			return err
		}
		if strings.HasPrefix(path, pages.Sessions) { // a digest, whose front matter fills the sessions table
			if err := session(tx, id, body); err != nil {
				return err
			}
		}
	}
	return tx.Commit()
}
