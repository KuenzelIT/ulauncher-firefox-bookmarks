import sqlite3
import tempfile
import shutil
import configparser
import os

class FirefoxHistory():
    def __init__(self, firefox_path: str):
        #   Results number
        self.limit = None

        #   Set history location
        self.history_location = self.searchPlaces(firefox_path)
        self.last_mtime = 0

        #   Temporary  file
        #   Using FF63 the DB was locked for exclusive use of FF
        self.temporary_history_location = tempfile.mktemp()
        self.conn = None
        self.update_db_if_needed()

    def update_db_if_needed(self):
        try:
            current_mtime = os.path.getmtime(self.history_location)
        except OSError:
            return

        if current_mtime > self.last_mtime:
            if self.conn:
                self.conn.close()
            shutil.copyfile(self.history_location, self.temporary_history_location)
            #   Open Firefox history database
            self.conn = sqlite3.connect(self.temporary_history_location, check_same_thread=False)
            #   External functions
            self.conn.create_function('hostname',1,self.__getHostname)
            self.last_mtime = current_mtime

    def searchPlaces(self, firefox_path: str):
        #   Firefox folder path
        firefox_path = os.path.expanduser(firefox_path)
        if not firefox_path.endswith("/"):
            firefox_path += "/"
        #   Firefox profiles configuration file path
        conf_path = os.path.join(firefox_path,'profiles.ini')
        #   Profile config parse
        profile = configparser.RawConfigParser()
        profile.read(conf_path)
        prof_path = profile.get("Profile0", "Path")
        #   Sqlite db directory path
        sql_path = os.path.join(firefox_path,prof_path)
        #   Sqlite db path
        return os.path.join(sql_path,'places.sqlite')

    #   Get hostname from url
    def __getHostname(self,str):
        url = str.split('/')
        if len(url)>2:
            return url[2]
        else:
            return 'Unknown'

    def search(self, term):
        self.update_db_if_needed()

        query = 'SELECT A.title, url FROM moz_bookmarks AS A'
        query += ' JOIN moz_places AS B ON(A.fk = B.id)'
        query += ' WHERE A.title LIKE "%%%s%%"' % term

        if term == "":
            query += ' ORDER BY A.lastModified DESC'
        else:
            query += ' ORDER BY instr(LOWER(A.title), LOWER("%s")) ASC' % term
        query += ' LIMIT %d' % self.limit


        #   Query execution
        cursor = self.conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        return rows

    def get_all_domains(self):
        self.update_db_if_needed()

        query = 'SELECT url FROM moz_bookmarks AS A'
        query += ' JOIN moz_places AS B ON(A.fk = B.id)'
        cursor = self.conn.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
        
        from urllib.parse import urlparse
        domains = set()
        for row in rows:
            url = row[0]
            if url:
                domain = urlparse(url).netloc
                if domain:
                    domains.add(domain)
        return list(domains)

    def close(self):
        self.conn.close()
