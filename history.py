import configparser
import os
import shutil
import sqlite3
import tempfile


class FirefoxHistory:
    def __init__(self):
        # Results number
        self.limit = None

        # Set history location
        history_location = self.searchPlaces()

        # Temporary file
        # Using FF63 the DB was locked for exclusive use of FF
        # TODO: Regular updates of the temporary file
        temporary_history_location = tempfile.mktemp()
        shutil.copyfile(history_location, temporary_history_location)
        # Open Firefox history database
        self.conn = sqlite3.connect(temporary_history_location)
        # External functions
        self.conn.create_function("hostname", 1, self.__getHostname)

    def searchPlaces(self):
        possible_paths = [
            os.path.join(os.environ["HOME"], ".mozilla/firefox"),
            os.path.join(os.environ["HOME"], "snap/firefox/common/.mozilla/firefox"),
            os.path.join(os.environ["HOME"], ".var/app/org.mozilla.firefox/.mozilla/firefox"),
        ]

        for firefox_path in possible_paths:
            conf_path = os.path.join(firefox_path, "profiles.ini")
            if os.path.exists(conf_path):
                # Profile config parse
                profile = configparser.RawConfigParser()
                profile.read(conf_path)
                prof_path = profile.get("Profile0", "Path")
                # Sqlite db directory path
                sql_path = os.path.join(firefox_path, prof_path)
                # Sqlite db path
                final_path = os.path.join(sql_path, "places.sqlite")
                if os.path.exists(final_path):
                    return final_path

        raise FileNotFoundError("Firefox history database not found in the searched paths.")

    # Get hostname from url
    def __getHostname(self, str):
        url = str.split("/")
        if len(url) > 2:
            return url[2]
        else:
            return "Unknown"

    def search(self, term):
        query = "SELECT A.title, url FROM moz_bookmarks AS A"
        query += " JOIN moz_places AS B ON(A.fk = B.id)"
        query += ' WHERE A.title LIKE "%%%s%%"' % term

        if term == "":
            query += " ORDER BY A.lastModified DESC"
        else:
            query += ' ORDER BY instr(LOWER(A.title), LOWER("%s")) ASC' % term
        query += " LIMIT %d" % self.limit

        # Query execution
        cursor = self.conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        return rows

    def close(self):
        self.conn.close()
