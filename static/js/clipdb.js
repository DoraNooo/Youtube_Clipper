/**
 * ClipDB — helper IndexedDB pour la bibliothèque YouTube Clipper.
 * Exposé en tant qu'objet global `ClipDB`.
 */
const ClipDB = (() => {
  const DB_NAME    = 'yt-clipper-v1';
  const DB_VERSION = 1;
  const STORE      = 'clips';
  let _db = null;

  function openDB() {
    if (_db) return Promise.resolve(_db);
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = ({ target: { result: db } }) => {
        if (!db.objectStoreNames.contains(STORE)) {
          const store = db.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
          store.createIndex('tags',       'tags',       { multiEntry: true });
          store.createIndex('created_at', 'created_at', { unique: false });
        }
      };
      req.onsuccess = ({ target: { result } }) => { _db = result; resolve(_db); };
      req.onerror   = ({ target: { error  } }) => reject(error);
    });
  }

  function run(mode, fn) {
    return openDB().then(db => new Promise((resolve, reject) => {
      const tx    = db.transaction(STORE, mode);
      const store = tx.objectStore(STORE);
      const req   = fn(store);
      if (req) {
        req.onsuccess = ({ target: { result } }) => resolve(result);
        req.onerror   = ({ target: { error  } }) => reject(error);
      } else {
        tx.oncomplete = () => resolve();
        tx.onerror    = ({ target: { error } }) => reject(error);
      }
    }));
  }

  /** Sauvegarde un clip. Retourne l'id auto-incrémenté. */
  function save(blob, { title = 'clip', filename = 'clip.mp4', tags = [], duration = 0 } = {}) {
    return run('readwrite', s => s.add({
      blob, title, filename, tags,
      duration, size: blob.size,
      created_at: new Date().toISOString(),
    }));
  }

  /** Retourne tous les clips (du plus récent au plus ancien). */
  function getAll() {
    return run('readonly', s => s.getAll())
      .then(clips => clips.sort((a, b) => b.created_at.localeCompare(a.created_at)));
  }

  /** Retourne un clip par id. */
  function get(id) {
    return run('readonly', s => s.get(id));
  }

  /** Met à jour les champs d'un clip (title, tags, …). */
  async function update(id, changes) {
    const db   = await openDB();
    const clip = await get(id);
    if (!clip) throw new Error(`Clip ${id} introuvable`);
    return new Promise((resolve, reject) => {
      const tx  = db.transaction(STORE, 'readwrite');
      const req = tx.objectStore(STORE).put({ ...clip, ...changes, id });
      req.onsuccess = () => resolve();
      req.onerror   = ({ target: { error } }) => reject(error);
    });
  }

  /** Supprime un clip par id. */
  function remove(id) {
    return run('readwrite', s => s.delete(id));
  }

  /** Retourne toutes les tags uniques de la bibliothèque. */
  function getAllTags() {
    return getAll().then(clips => {
      const set = new Set();
      clips.forEach(c => (c.tags || []).forEach(t => set.add(t)));
      return [...set].sort();
    });
  }

  /** Estimation du stockage utilisé (si disponible). */
  function storageInfo() {
    if (!navigator.storage?.estimate) return Promise.resolve(null);
    return navigator.storage.estimate();
  }

  return { save, getAll, get, update, remove, getAllTags, storageInfo };
})();
