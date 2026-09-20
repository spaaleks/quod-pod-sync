/*
 * ipod-db: reads and writes an iPod database through libgpod.
 *
 * Takes tab separated commands on stdin and prints tab separated records, so the Python side stays free
 * of libgpod. The database is written once, after all commands ran.
 */
#include <gpod/itdb.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_FIELDS 20

static void print_clean(const char *s) {
    for (; s && *s; s++)
        putchar(*s == '\t' || *s == '\n' || *s == '\r' ? ' ' : *s);
}

static Itdb_Track *find_track(Itdb_iTunesDB *db, guint64 dbid) {
    for (GList *l = db->tracks; l; l = l->next) {
        Itdb_Track *t = l->data;
        if (t->dbid == dbid)
            return t;
    }
    return NULL;
}

static Itdb_Playlist *find_playlist(Itdb_iTunesDB *db, const char *name) {
    for (GList *l = db->playlists; l; l = l->next) {
        Itdb_Playlist *pl = l->data;
        if (!itdb_playlist_is_mpl(pl) && !pl->is_spl && pl->name && strcmp(pl->name, name) == 0)
            return pl;
    }
    return NULL;
}

static guint64 new_dbid(Itdb_iTunesDB *db) {
    guint64 id;
    do {
        id = ((guint64)g_random_int() << 32) | g_random_int();
    } while (id == 0 || find_track(db, id));
    return id;
}

static int cmd_list(Itdb_iTunesDB *db) {
    for (GList *l = db->tracks; l; l = l->next) {
        Itdb_Track *t = l->data;
        printf("T\t%llu\t%u\t%d\t%d\t%d\t%u\t%u\t%u\t%u\t", (unsigned long long)t->dbid, t->size, t->tracklen,
               t->cd_nr, t->track_nr, t->rating, t->app_rating, t->playcount, t->recent_playcount);
        const char *fields[] = {t->ipod_path, t->title, t->artist, t->albumartist, t->album};
        for (int i = 0; i < 5; i++) {
            print_clean(fields[i]);
            putchar(i < 4 ? '\t' : '\n');
        }
    }
    for (GList *l = db->playlists; l; l = l->next) {
        Itdb_Playlist *pl = l->data;
        if (itdb_playlist_is_mpl(pl) || pl->is_spl || itdb_playlist_is_podcasts(pl))
            continue;
        fputs("P\t", stdout);
        print_clean(pl->name);
        putchar('\t');
        for (GList *m = pl->members; m; m = m->next)
            printf("%s%llu", m == pl->members ? "" : ",", (unsigned long long)((Itdb_Track *)m->data)->dbid);
        putchar('\n');
    }
    return 0;
}

static char *nonempty(const char *s) { return s && *s ? g_strdup(s) : NULL; }

static int do_add(Itdb_iTunesDB *db, char **f, int n) {
    if (n < 19) {
        fprintf(stderr, "add: expected 19 fields, got %d\n", n);
        return -1;
    }
    Itdb_Track *t = itdb_track_new();
    t->filetype = g_strdup(f[3]);
    t->title = nonempty(f[4]);
    t->artist = nonempty(f[5]);
    t->albumartist = nonempty(f[6]);
    t->album = nonempty(f[7]);
    t->genre = nonempty(f[8]);
    t->composer = nonempty(f[9]);
    t->year = atoi(f[10]);
    t->track_nr = atoi(f[11]);
    t->tracks = atoi(f[12]);
    t->cd_nr = atoi(f[13]);
    t->cds = atoi(f[14]);
    t->tracklen = atoi(f[15]);
    t->bitrate = atoi(f[16]);
    t->samplerate = (guint16)atoi(f[17]);
    t->compilation = (guint8)atoi(f[18]);
    t->mediatype = ITDB_MEDIATYPE_AUDIO;
    t->time_added = t->time_modified = time(NULL);
    t->dbid = new_dbid(db);

    itdb_track_add(db, t, -1);
    GError *err = NULL;
    if (!itdb_cp_track_to_ipod(t, f[1], &err)) {
        fprintf(stderr, "add: copy failed for %s: %s\n", f[1], err ? err->message : "unknown error");
        g_clear_error(&err);
        itdb_track_remove(t);
        return -1;
    }
    itdb_playlist_add_track(itdb_playlist_mpl(db), t, -1);
    if (*f[2] && !itdb_track_set_thumbnails(t, f[2]))
        fprintf(stderr, "add: could not set cover for %s\n", f[1]);
    printf("added\t%llu\t%s\n", (unsigned long long)t->dbid, f[1]);
    return 0;
}

static int do_remove(Itdb_iTunesDB *db, const char *id) {
    Itdb_Track *t = find_track(db, g_ascii_strtoull(id, NULL, 10));
    if (!t) {
        fprintf(stderr, "remove: no track with dbid %s\n", id);
        return -1;
    }
    gchar *path = itdb_filename_on_ipod(t);
    for (GList *l = db->playlists; l; l = l->next)
        while (itdb_playlist_contains_track(l->data, t))
            itdb_playlist_remove_track(l->data, t);
    itdb_track_remove(t);
    if (path) {
        unlink(path);
        g_free(path);
    }
    return 0;
}

/* rating/playcount from the library; app_rating marks it as synced so later changes on the iPod are visible */
static int do_rated(Itdb_iTunesDB *db, char **f, int n) {
    if (n < 4) {
        fprintf(stderr, "rated: expected 4 fields, got %d\n", n);
        return -1;
    }
    Itdb_Track *t = find_track(db, g_ascii_strtoull(f[1], NULL, 10));
    if (!t) {
        fprintf(stderr, "rated: no track with dbid %s\n", f[1]);
        return -1;
    }
    t->rating = t->app_rating = (guint32)atoi(f[2]);
    t->playcount = (guint32)atoi(f[3]);
    t->recent_playcount = 0;
    t->time_modified = time(NULL);
    return 0;
}

static int do_playlist(Itdb_iTunesDB *db, const char *name, const char *ids) {
    Itdb_Playlist *old = find_playlist(db, name);
    if (old)
        itdb_playlist_remove(old);
    Itdb_Playlist *pl = itdb_playlist_new(name, FALSE);
    itdb_playlist_add(db, pl, -1);
    gchar **parts = g_strsplit(ids ? ids : "", ",", -1);
    for (gchar **p = parts; *p; p++) {
        if (!**p)
            continue;
        Itdb_Track *t = find_track(db, g_ascii_strtoull(*p, NULL, 10));
        if (t)
            itdb_playlist_add_track(pl, t, -1);
        else
            fprintf(stderr, "playlist: no track with dbid %s\n", *p);
    }
    g_strfreev(parts);
    return 0;
}

static int cmd_apply(Itdb_iTunesDB *db) {
    char *line = NULL;
    size_t cap = 0;
    ssize_t len;
    int failures = 0;
    while ((len = getline(&line, &cap, stdin)) != -1) {
        while (len > 0 && (line[len - 1] == '\n' || line[len - 1] == '\r'))
            line[--len] = '\0';
        if (!len)
            continue;
        char *f[MAX_FIELDS] = {0};
        int n = 0;
        for (char *s = line; s && n < MAX_FIELDS; n++) {
            f[n] = s;
            s = strchr(s, '\t');
            if (s)
                *s++ = '\0';
        }
        int rc;
        if (!strcmp(f[0], "add"))
            rc = do_add(db, f, n);
        else if (!strcmp(f[0], "rated"))
            rc = do_rated(db, f, n);
        else if (!strcmp(f[0], "remove") && n >= 2)
            rc = do_remove(db, f[1]);
        else if (!strcmp(f[0], "playlist") && n >= 2)
            rc = do_playlist(db, f[1], n >= 3 ? f[2] : "");
        else if (!strcmp(f[0], "delplaylist") && n >= 2) {
            Itdb_Playlist *pl = find_playlist(db, f[1]);
            if (pl)
                itdb_playlist_remove(pl);
            rc = 0;
        } else {
            fprintf(stderr, "unknown command: %s\n", f[0]);
            rc = -1;
        }
        failures += rc != 0;
        fflush(stdout);
    }
    free(line);

    GError *err = NULL;
    if (!itdb_write(db, &err)) {
        fprintf(stderr, "write failed: %s\n", err ? err->message : "unknown error");
        return 2;
    }
    printf("written\t%u tracks\t%d failures\n", g_list_length(db->tracks), failures);
    return failures ? 1 : 0;
}

int main(int argc, char **argv) {
    if (argc != 3 || (strcmp(argv[2], "list") && strcmp(argv[2], "apply"))) {
        fprintf(stderr, "usage: %s MOUNTPOINT list|apply\n", argv[0]);
        return 64;
    }
    GError *err = NULL;
    Itdb_iTunesDB *db = itdb_parse(argv[1], &err);
    if (!db) {
        fprintf(stderr, "cannot read iPod database at %s: %s\n", argv[1], err ? err->message : "unknown error");
        return 3;
    }
    int rc = !strcmp(argv[2], "list") ? cmd_list(db) : cmd_apply(db);
    itdb_free(db);
    return rc;
}
