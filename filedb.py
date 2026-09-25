# filedb.py

import json
import os
import logging
import threading
from datetime import datetime
from typing import List, Dict, Optional, Set

USERS_FILE = 'users.json'
ALLOWED_USERS_FILE = 'allowed_users.json'

# --- Thread-safe Lock ---
# This lock prevents race conditions where two users writing to the file at the same time could corrupt it.
_file_lock = threading.Lock()

# In-memory cache for allowed user IDs for performance
_allowed_user_ids_cache: Set[int] = set()


def load_allowed_users_cache():
    """Loads allowed user IDs from the file into the in-memory cache."""
    global _allowed_user_ids_cache
    logging.info("Loading allowed users into cache...")
    with _file_lock: # Ensure we don't read while another thread is writing
        allowed_users = _load_json_internal(ALLOWED_USERS_FILE)
    _allowed_user_ids_cache = {user['id'] for user in allowed_users}
    logging.info(f"Loaded {len(_allowed_user_ids_cache)} allowed users into cache.")


# --- Internal (unsafe) I/O functions ---

def _load_json_internal(filename: str) -> List[Dict]:
    """Internal function to load JSON without a lock. Assumes lock is held by caller."""
    if not os.path.exists(filename):
        return []
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        logging.error(f"Could not read or parse {filename}, returning empty list.")
        return []

def _save_json_internal(filename: str, data: List[Dict]):
    """Internal function to save JSON without a lock. Assumes lock is held by caller."""
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# --- Public Thread-safe Functions ---

def upsert_user(user_id: int, username: str, first_name: str,
                last_name: str = "") -> bool:
    """Records a user, refreshing stored names to the newest seen values.

    New users are appended with ``date_joined``; existing users keep all
    their stored fields (including ``date_joined`` and any unknown extras)
    while ``username``/``first_name``/``last_name`` are updated so admin
    views and access requests always show current data. The optional
    ``last_name`` keeps older 3-argument calls working. Thread-safe.
    """
    with _file_lock:
        users = _load_json_internal(USERS_FILE)
        for existing in users:
            if existing.get('id') == user_id:
                existing['username'] = username or ''
                existing['first_name'] = first_name or ''
                if last_name or 'last_name' not in existing:
                    existing['last_name'] = last_name or ''
                _save_json_internal(USERS_FILE, users)
                return True

        user_entry = {
            'id': user_id,
            'username': username or '',
            'first_name': first_name or '',
            'last_name': last_name or '',
            'date_joined': datetime.now().isoformat()
        }
        users.append(user_entry)
        _save_json_internal(USERS_FILE, users)
    return True

def get_user_by_id(user_id: int) -> Optional[Dict]:
    """Gets a user by their ID from the main list. Thread-safe."""
    with _file_lock:
        users = _load_json_internal(USERS_FILE)
    return next((user for user in users if user['id'] == user_id), None)

def list_all_users() -> List[Dict]:
    """Lists all users from the main list. Thread-safe."""
    with _file_lock:
        return _load_json_internal(USERS_FILE)

def add_allowed_user_from_user(user: Dict) -> bool:
    """Adds a user to the allowed list and updates the cache. Thread-safe."""
    user_id = user['id']
    if user_id in _allowed_user_ids_cache:
        return True

    with _file_lock:
        allowed_list = _load_json_internal(ALLOWED_USERS_FILE)
        if any(u['id'] == user_id for u in allowed_list):
             # Cache was out of sync, update it and return
            _allowed_user_ids_cache.add(user_id)
            return True

        allowed_list.append(user)
        _save_json_internal(ALLOWED_USERS_FILE, allowed_list)
    
    _allowed_user_ids_cache.add(user_id) # Update cache after successful write
    return True

def list_allowed_users() -> List[Dict]:
    """Lists all allowed users. Thread-safe."""
    with _file_lock:
        return _load_json_internal(ALLOWED_USERS_FILE)

def remove_allowed_user(user_id: int) -> bool:
    """Removes a user from the allowed list and updates the cache. Thread-safe."""
    if user_id not in _allowed_user_ids_cache:
        return False # Not in cache, so not in file either. Fast path.

    with _file_lock:
        allowed_list = _load_json_internal(ALLOWED_USERS_FILE)
        initial_count = len(allowed_list)
        new_allowed = [u for u in allowed_list if u['id'] != user_id]

        if len(new_allowed) == initial_count:
            # This can happen if cache is stale. Remove from cache and report failure.
            if user_id in _allowed_user_ids_cache:
                _allowed_user_ids_cache.remove(user_id)
            return False

        _save_json_internal(ALLOWED_USERS_FILE, new_allowed)

    # Update cache after successful write
    if user_id in _allowed_user_ids_cache:
        _allowed_user_ids_cache.remove(user_id)
    return True


def is_user_allowed(user_id: int) -> bool:
    """Checks if a user is allowed using the fast in-memory cache."""
    # This function is read-only on the cache, so it doesn't need a lock.
    return user_id in _allowed_user_ids_cache