#!/data/data/com.termux/files/usr/bin/python3
import os
os.environ["OPENBLAS_NUM_THREADS"] = "1"

import re
import json
import time
import requests
import hashlib
import feedparser
import fasttext
import logging
from xml.etree import ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from git import Repo, exc
import datetime

# ========== CONFIGURAZIONE PERSONALIZZABILE ==========
CACHE_FILE = os.path.expanduser("~/rss_project/processed_articles.json")
OPML_FILE = os.path.expanduser("/storage/emulated/0/RSS/Inoreader Feeds 20250130.opml")
GITHUB_REPO_PATH = "/storage/emulated/0/Feed-Rss-notizie"
GITHUB_REPO_URL = "git@github.com:Snapcyberdragon/Feed-Rss-notizie.git"
CATEGORIZED_FEEDS_DIR = os.path.join(GITHUB_REPO_PATH, "categorized_feeds")

# Parametri prestazionali
MAX_THREADS = 3
REQUEST_TIMEOUT = 10
CACHE_EXPIRE_DAYS = 3
FEED_LIMIT = 20
ARTICLES_PER_FEED = 10
CHECK_INTERVAL = 3600  # 1 ora
OPML_REFRESH_INTERVAL = 86400  # 24 ore

# Configurazione logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.expanduser("~/rss_project/rss.log")),
        logging.StreamHandler()
    ]
)

# Regex precompilate
CLEAN_REGEX = re.compile(r'<[^>]+>|http\S+|[^\w\s]')
SPACES_REGEX = re.compile(r'\s+')
NEWLINES_REGEX = re.compile(r'[\n\r\t]+')

# Caricamento modello lingua
lid_model = fasttext.load_model(os.path.expanduser("~/lid.176.ftz"))

# ========== CATEGORIE PERSONALIZZABILI ==========
CATEGORIES = {
    "Italia": {
        "keywords": {
            re.compile(r'\b(italia|italy)\b', re.I): 3,
            re.compile(r'\b(roma|rome|milano|milan)\b', re.I): 2,
            re.compile(r'\b(governo|senato|camera|parlamento)\b', re.I): 4
        },
        "exclude": [
            re.compile(r'\b(ue|eu|nato|europa)\b', re.I)
        ],
        "threshold": 5
    },
    "Economy": {
        "keywords": {
            re.compile(r'\b(pil|gdp)\b', re.I): 4,
            re.compile(r'\b(inflazione|inflation)\b', re.I): 3,
            re.compile(r'\b(spread|bce|ecb)\b', re.I): 4
        },
        "threshold": 6
    },
    "USA": {
        "keywords": {
            re.compile(r'\b(usa|u\.s\.a|united states)\b', re.I): 5,
            re.compile(r'\b(white house|congresso usa)\b', re.I): 4
        },
        "threshold": 4
    }
}

class FeedProcessor:
    def __init__(self):
        self.cache = self.load_cache()
        self.last_fetch = {}
        self.feed_timeout = {}
        self.last_opml_update = 0
        self.feeds_list = []
        self.repo = None
        self.setup_git_repo()

    def setup_git_repo(self):
        """Configura il repository GitHub con fallback"""
        try:
            if not os.path.exists(GITHUB_REPO_PATH):
                os.makedirs(GITHUB_REPO_PATH, exist_ok=True)

            try:
                self.repo = Repo(GITHUB_REPO_PATH)
            except exc.InvalidGitRepositoryError:
                logging.info("Inizializzazione nuovo repository Git...")
                self.repo = Repo.init(GITHUB_REPO_PATH)
                origin = self.repo.create_remote('origin', GITHUB_REPO_URL)
                origin.fetch()

            # Configura SSH
            ssh_cmd = f'ssh -i {os.path.expanduser("~/.ssh/github")}'
            self.repo.git.update_environment(GIT_SSH_COMMAND=ssh_cmd)

            # Configura utente
            with self.repo.config_writer() as config:
                config.set_value("user", "name", "Snapcyberdragon")
                config.set_value("user", "email", "tuamail@esempio.com")

        except Exception as e:
            logging.error(f"Git setup error: {str(e)}")
            self.repo = None

    def load_cache(self):
        try:
            with open(CACHE_FILE, 'r') as f:
                cache = json.load(f)
                now = time.time()
                return {k: v for k, v in cache.items() 
                        if (now - v['timestamp']) < CACHE_EXPIRE_DAYS * 86400}
        except Exception:
            return {}

    def save_cache(self):
        try:
            with open(CACHE_FILE, 'w') as f:
                json.dump(self.cache, f, indent=2)
        except Exception as e:
            logging.error(f"Save cache error: {str(e)}")

    def update_opml(self):
        """Aggiorna la lista dei feed da GitHub con fallback locale"""
        try:
            # Prova a scaricare da GitHub
            response = requests.get(
                "https://raw.githubusercontent.com/Snapcyberdragon/Feed-Rss-notizie/main/feeds.opml",
                timeout=10
            )
            response.raise_for_status()
            
            with open(OPML_FILE, 'w') as f:
                f.write(response.text)
            
            logging.info("✅ OPML aggiornato da GitHub")
            self.last_opml_update = time.time()

        except Exception as e:
            logging.warning(f"GitHub OPML error: {str(e)}, usando file locale")
            if not os.path.exists(OPML_FILE):
                self.create_default_opml()

        try:
            tree = ET.parse(OPML_FILE)
            self.feeds_list = [
                outline.attrib['xmlUrl'] 
                for outline in tree.iter('outline') 
                if 'xmlUrl' in outline.attrib
            ][:FEED_LIMIT]
            
        except Exception as e:
            logging.error(f"OPML parse error: {str(e)}")
            self.feeds_list = [
                "https://www.ansa.it/sito/ansait_rss.xml",
                "https://www.repubblica.it/rss.xml"
            ]

    def create_default_opml(self):
        """Crea un file OPML di default se mancante"""
        default_opml = '''<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
<head>
  <title>Feed Predefiniti</title>
</head>
<body>
  <outline text="ANSA" title="ANSA" 
           type="rss" 
           xmlUrl="https://www.ansa.it/sito/ansait_rss.xml"/>
  <outline text="Repubblica" title="Repubblica" 
           type="rss" 
           xmlUrl="https://www.repubblica.it/rss.xml"/>
</body>
</opml>'''
        with open(OPML_FILE, 'w') as f:
            f.write(default_opml)
        logging.info("Creato file OPML predefinito")

    def parse_opml(self):
        if not self.feeds_list or time.time() - self.last_opml_update > OPML_REFRESH_INTERVAL:
            self.update_opml()
        return self.feeds_list

    def process_entry(self, entry):
        try:
            raw_text = f"{entry.get('title', '')} {entry.get('description', '')}"
            content_hash = hashlib.sha256(raw_text.encode()).hexdigest()
            
            if content_hash in self.cache:
                return None
                
            category = self.classify_text(raw_text)
            
            self.cache[content_hash] = {
                'timestamp': time.time(),
                'category': category,
                'title': entry.get('title', '')[:200],
                'link': entry.get('link', '')
            }
            
            return category
        except Exception as e:
            logging.error(f"Process entry error: {str(e)}")
            return None

    def classify_text(self, text):
        try:
            text_clean = NEWLINES_REGEX.sub(' ', str(text))
            text_clean = CLEAN_REGEX.sub(' ', text_clean)
            text_clean = SPACES_REGEX.sub(' ', text_clean).lower().strip()
            
            if not text_clean:
                return "Altro"

            scores = {cat: 0 for cat in CATEGORIES}
            excluded = set()

            # Controllo esclusioni
            for cat, config in CATEGORIES.items():
                for pattern in config.get("exclude", []):
                    if pattern.search(text_clean):
                        excluded.add(cat)
                        break

            # Calcolo punteggi
            for cat, config in CATEGORIES.items():
                if cat in excluded:
                    continue
                for pattern, weight in config["keywords"].items():
                    if pattern.search(text_clean):
                        scores[cat] += weight

            # Determina categoria
            valid_cats = {cat: score for cat, score in scores.items() 
                         if score >= CATEGORIES[cat].get("threshold", 0)}
            return max(valid_cats, key=valid_cats.get) if valid_cats else "Altro"
            
        except Exception as e:
            logging.error(f"Classification error: {str(e)}")
            return "Altro"

    def fetch_feed(self, feed_url):
        try:
            if self.feed_timeout.get(feed_url, 0) > time.time():
                return []
                
            headers = {}
            if feed_url in self.last_fetch:
                headers['If-Modified-Since'] = formatdate(self.last_fetch[feed_url])
            
            response = requests.get(feed_url, headers=headers, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 304:
                return []
                
            feed_data = feedparser.parse(response.content)
            self.last_fetch[feed_url] = time.time()
            return feed_data.entries[:ARTICLES_PER_FEED]
            
        except Exception as e:
            logging.error(f"Feed error {feed_url}: {str(e)}")
            self.feed_timeout[feed_url] = time.time() + 300  # 5 min timeout
            return []

    def generate_opml(self, category):
        try:
            opml_content = '''<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
<head>
<title>{} Feed</title>
</head>
<body>\n'''.format(category)
            
            count = 0
            for entry in self.cache.values():
                if entry['category'] == category:
                    opml_content += f'<outline type="rss" text="{entry["title"]}" title="{entry["title"]}" xmlUrl="{entry["link"]}"/>\n'
                    count += 1
                    if count >= 100:
                        break
            
            opml_content += "</body>\n</opml>"
            
            os.makedirs(CATEGORIZED_FEEDS_DIR, exist_ok=True)
            filename = os.path.join(CATEGORIZED_FEEDS_DIR, f"{category.lower()}_feeds.opml")
            with open(filename, 'w') as f:
                f.write(opml_content)
            return filename
        except Exception as e:
            logging.error(f"OPML generation error: {str(e)}")
            return None

    def push_to_github(self):
        if not self.repo:
            logging.warning("Repository Git non disponibile")
            return
            
        try:
            self.repo.git.add(all=True)
            self.repo.index.commit(f"Aggiornamento automatico {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
            self.repo.remote().push()
            logging.info("✅ Push su GitHub completato")
        except Exception as e:
            logging.error(f"GitHub push error: {str(e)}")


def main():
    processor = FeedProcessor()
    last_push = time.time()
    
    try:
        while True:
            start_time = time.time()
            logging.debug("=== NUOVO CICLO ===")
            
            # Fase 1: Fetch feed
            logging.debug(f"[FASE 1] Fetching da {len(processor.parse_opml())} feed...")
            with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                feeds = processor.parse_opml()
                logging.debug(f"Feed da processare: {feeds}")
                future_to_url = {executor.submit(processor.fetch_feed, url): url for url in feeds}
                entries = []
                
                for future in as_completed(future_to_url):
                    result = future.result()
                    logging.debug(f"Feed {future_to_url[future]} restituiti {len(result)} articoli")
                    entries.extend(result)
            logging.debug(f"[FASE 1 COMPLETATA] Articoli totali: {len(entries)}")

            # Fase 2: Processamento
            logging.debug("[FASE 2] Inizio processamento articoli...")
            with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                futures = [executor.submit(processor.process_entry, entry) for entry in entries]
                processed = 0
                
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        processed += 1
                        logging.debug(f"Articolo processato: {result}")
                        
                processor.save_cache()
            logging.debug(f"[FASE 2 COMPLETATA] Processati {processed}/{len(entries)} articoli")

            # Fase 3: Push GitHub
            logging.debug("[FASE 3] Verifica push GitHub...")
            current_time = time.time()
            time_since_last_push = current_time - last_push
            logging.debug(f"Tempo dall'ultimo push: {time_since_last_push}s")
            
            if time_since_last_push > 21600:
                logging.debug("Inizio push GitHub...")
                try:
                    processor.push_to_github()
                    last_push = time.time()
                    logging.debug("Push GitHub completato con successo")
                    logging.debug("Stato repository:")
                    logging.debug(processor.repo.git.status())
                except Exception as e:
                    logging.error(f"Errore durante il push: {str(e)}")
                    logging.debug("Stato repository fallito:")
                    logging.debug(processor.repo.git.status())
            else:
                logging.debug("Push GitHub non necessario")

            # Gestione intervallo
            elapsed = time.time() - start_time
            sleep_time = max(CHECK_INTERVAL - elapsed, 60)
            logging.debug(f"Tempo ciclo: {elapsed:.2f}s - Sleep per {sleep_time}s")
            logging.debug("=== FINE CICLO ===")
            
            time.sleep(sleep_time)
            
    except KeyboardInterrupt:
        logging.debug("Interruzione manuale rilevata")
        logging.debug("Tentativo push finale...")
        processor.push_to_github()
        logging.debug("Push finale completato")
        logging.info("⏹️  Interruzione manuale")
    
    except Exception as e:
        logging.error(f"Errore critico: {str(e)}", exc_info=True)
        os._exit(1)

if __name__ == "__main__":
    main()
