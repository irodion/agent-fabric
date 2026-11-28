// Package pages is a workspace's pages as kb-go finds them (docs/design.md §4.6, §4.9): the Markdown files under a
// few folders of its wiki and of its local digests, each named by its path there, wherever it's kept.
package pages

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

// Kind is a kind of page, and the folder its pages are in.
type Kind struct{ Name, Folder string }

// Kinds are the kinds of page, by folder: the session digests, the wiki's own pages, and the notes it filed. Not its
// manifests or catalogue, which are ingest's bookkeeping.
var Kinds = []Kind{{"session", Sessions}, {"page", "wiki/"}, {"note", "raw/notes/"}}

// Sessions is where the session digests are.
const Sessions = "raw/sessions/"

// IsPage says whether a path names a page: a Markdown file under a kind's folder, by a clean path that stays there.
func IsPage(rel string) bool {
	if !filepath.IsLocal(rel) || filepath.Clean(rel) != rel || !strings.HasSuffix(rel, ".md") {
		return false
	}
	for _, kind := range Kinds {
		if strings.HasPrefix(rel, kind.Folder) {
			return true
		}
	}
	return false
}

// Read reads a page in one folder, by a path that has to stay there. Anything but a file there, a link or a folder
// say, is no page there: fs.ErrNotExist, as is a link or a file where a folder of the path should be.
func Read(folder, rel string) ([]byte, error) {
	root, err := os.OpenRoot(folder)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	// Each folder of the path, and the page, as it is: a link anywhere along it is no page, as Walk follows none.
	parts := strings.Split(rel, "/")
	for i := range parts {
		info, err := root.Lstat(strings.Join(parts[:i+1], "/"))
		if errors.Is(err, syscall.ENOTDIR) {
			return nil, fs.ErrNotExist
		}
		if err != nil {
			return nil, err
		}
		last := i == len(parts)-1
		if (last && !info.Mode().IsRegular()) || (!last && !info.IsDir()) {
			return nil, fs.ErrNotExist
		}
	}
	return root.ReadFile(rel)
}

// Find reads a page in the first of the folders that holds it, and that it may read: one it may not is passed over,
// as Walk passes it over, so that a page search lists is the one get_page gives.
func Find(folders []string, rel string) ([]byte, error) {
	for _, folder := range folders {
		data, err := Read(folder, rel)
		if !errors.Is(err, fs.ErrNotExist) && !errors.Is(err, fs.ErrPermission) {
			return data, err
		}
	}
	return nil, fs.ErrNotExist
}

// skip leaves out a page, or a whole folder, of a walk.
func skip(entry fs.DirEntry) error {
	if entry != nil && entry.IsDir() {
		return filepath.SkipDir
	}
	return nil
}

// Walk calls `visit` once for each page the folders hold, with the folder that holds it first and its file's facts.
// A folder that isn't there holds none; links in it aren't followed, as Read doesn't serve them, but a folder that is
// itself a link, as a wiki kept elsewhere may be, is walked where it leads, as Read reads in it.
func Walk(folders []string, visit func(rel, folder string, info fs.FileInfo)) error {
	seen := map[string]bool{}
	for _, folder := range folders {
		resolved, err := filepath.EvalSymlinks(folder)
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			return err
		}
		err = filepath.WalkDir(resolved, func(file string, entry fs.DirEntry, err error) error {
			if err != nil && errors.Is(err, fs.ErrPermission) {
				// One page or folder that can't be read, the page folder itself too, spoils no search of the rest,
				// as Find passes it over.
				return skip(entry)
			}
			if err != nil {
				if errors.Is(err, fs.ErrNotExist) {
					return nil
				}
				return err
			}
			rel, _ := filepath.Rel(resolved, file)
			if entry.IsDir() {
				if rel == "." || mayHold(rel) {
					return nil
				}
				return filepath.SkipDir // as the wiki's .git, which every search would read through otherwise
			}
			if !entry.Type().IsRegular() || !IsPage(rel) || seen[rel] {
				return nil
			}
			info, err := entry.Info()
			if err != nil {
				return nil // gone since the listing
			}
			// A copy that can't be read is passed over, as Find passes it over, so the page found is the one Find
			// gives: a later folder's copy, or none, so that the index drops one made unreadable since, though its
			// size and time are as they were.
			opened, err := os.Open(file)
			if err != nil {
				return nil
			}
			opened.Close()
			seen[rel] = true
			visit(rel, folder, info)
			return nil
		})
		if err != nil {
			return err
		}
	}
	return nil
}

// mayHold says whether a folder, by its path in a page folder, is or leads to a kind's folder.
func mayHold(rel string) bool {
	for _, kind := range Kinds {
		if strings.HasPrefix(rel+"/", kind.Folder) || strings.HasPrefix(kind.Folder, rel+"/") {
			return true
		}
	}
	return false
}
