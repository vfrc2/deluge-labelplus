#
# core.py
#
# Copyright (C) 2013 Ratanak Lun <ratanakvlun@gmail.com>
# Copyright (C) 2008 Martijn Voncken <mvoncken@gmail.com>
#
# Basic plugin template created by:
# Copyright (C) 2008 Martijn Voncken <mvoncken@gmail.com>
# Copyright (C) 2007-2009 Andrew Resch <andrewresch@gmail.com>
# Copyright (C) 2009 Damien Churchill <damoxc@gmail.com>
#
# Deluge is free software.
#
# You may redistribute it and/or modify it under the terms of the
# GNU General Public License, as published by the Free Software
# Foundation; either version 3 of the License, or (at your option)
# any later version.
#
# deluge is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with deluge.    If not, write to:
#   The Free Software Foundation, Inc.,
#   51 Franklin Street, Fifth Floor
#   Boston, MA  02110-1301, USA.
#
#    In addition, as a special exception, the copyright holders give
#    permission to link the code of portions of this program with the OpenSSL
#    library.
#    You must obey the GNU General Public License in all respects for all of
#    the code used other than OpenSSL. If you modify file(s) with this
#    exception, you may extend this exception to your version of the file(s),
#    but you are not obligated to do so. If you do not wish to do so, delete
#    this exception statement from your version. If you delete this exception
#    statement from all source files in the program, then also delete it here.
#


import os.path
import cPickle
import datetime

import deluge.common
import deluge.configmanager
from deluge import component
from deluge.log import LOG as log
from deluge.core.rpcserver import export
from deluge.plugins.pluginbase import CorePluginBase

import common.validation as Validation
import common.label as Label
from common.debug import debug

from common.constant import PLUGIN_NAME, MODULE_NAME
from common.constant import CORE_CONFIG
from common.constant import STATUS_ID, STATUS_NAME
from common.constant import OPTION_DEFAULTS, LABEL_DEFAULTS
from common.constant import NULL_PARENT, ID_ALL, ID_NONE
from common.constant import RESERVED_IDS


CONFIG_DEFAULTS = {
  "prefs": {
    "options": dict(OPTION_DEFAULTS),
    "defaults": dict(LABEL_DEFAULTS),
  },

  "labels": {},   # "label_id": {"name": str, "data": dict}
  "mappings": {}, # "torrent_id": "label_id"
}


def init_check(func):


  def wrap(*args, **kwargs):

    if len(args) > 0 and isinstance(args[0], Core):
      Validation.require(args[0].initialized, "Plugin not initialized")

    return func(*args, **kwargs)


  return wrap


class Core(CorePluginBase):


  def __init__(self, plugin_name):

    super(Core, self).__init__(plugin_name)
    self.initialized = False


  def enable(self):

    log.debug("[%s] Initializing Core", PLUGIN_NAME)

    self._core = deluge.configmanager.ConfigManager("core.conf")
    self._config = deluge.configmanager.ConfigManager(
        CORE_CONFIG, defaults=CONFIG_DEFAULTS)

    self._prefs = self._config["prefs"]

    self._labels = self._config["labels"]
    self._mappings = self._config["mappings"]

    if not component.get("TorrentManager").session_started:
      component.get("EventManager").register_event_handler(
          "SessionStartedEvent", self._initialize)
      log.debug("[%s] Waiting for session to start...", PLUGIN_NAME)
    else:
      self._initialize()


  def disable(self):

    log.debug("[%s] Deinitializing Core", PLUGIN_NAME)

    component.get("EventManager").deregister_event_handler(
        "SessionStartedEvent", self._initialize)

    self.initialized = False

    self._config.save()
    deluge.configmanager.close(self._config)

    component.get("EventManager").deregister_event_handler(
        "TorrentAddedEvent", self.on_torrent_added)
    component.get("EventManager").deregister_event_handler(
        "PreTorrentRemovedEvent", self.on_torrent_removed)

    component.get("AlertManager").deregister_handler(
        self.on_torrent_finished)

    component.get("CorePluginManager").deregister_status_field(STATUS_ID)
    component.get("CorePluginManager").deregister_status_field(STATUS_NAME)

    component.get("FilterManager").deregister_filter(STATUS_ID)

    self._rpc_deregister(PLUGIN_NAME)

    log.debug("[%s] Core deinitialized", PLUGIN_NAME)


  @export
  def is_initialized(self):

    return self.initialized


  @export
  @init_check
  @debug()
  def add_label(self, parent_id, label_name):

    Validation.require(parent_id in self._labels, "Unknown Label")

    label_name = label_name.strip()
    self._validate_name(parent_id, label_name)

    id = self._get_unused_id(parent_id)
    self._index[parent_id]["children"].append(id)

    self._labels[id] = {
      "name": label_name,
      "data": dict(self._prefs["defaults"]),
    }

    self._index[id] = {
      "children": [],
      "torrents": [],
    }

    options = self._labels[id]["data"]
    mode = options["move_data_completed_mode"]
    if mode != "folder":
      path = self.get_parent_path(id)

      if mode == "subfolder":
        path = os.path.join(path, label_name)

      options["move_data_completed_path"] = path

    self._build_label_ancestry(id)

    self._last_modified = datetime.datetime.now()
    self._config.save()

    return id


  @export
  @init_check
  @debug()
  def remove_label(self, label_id):

    Validation.require(label_id not in RESERVED_IDS and
        label_id in self._labels, "Unknown Label")

    self._remove_label(label_id)

    parent_id = Label.get_parent(label_id)
    self._index[parent_id]["children"].remove(label_id)

    self._last_modified = datetime.datetime.now()
    self._config.save()


  @export
  @init_check
  @debug()
  def rename_label(self, label_id, label_name):

    Validation.require(label_id not in RESERVED_IDS and
        label_id in self._labels, "Unknown Label")

    label_name = label_name.strip()
    self._validate_name(Label.get_parent(label_id), label_name)

    obj = self._labels[label_id]
    obj["name"] = label_name

    self._clear_subtree_ancestry(label_id)

    if obj["data"]["move_data_completed_mode"] == "subfolder":
      path = os.path.join(self.get_parent_path(label_id), label_name)
      obj["data"]["move_data_completed_path"] = path

      self._apply_data_completed_path(label_id)
      self._propagate_path_to_descendents(label_id)

    self._last_modified = datetime.datetime.now()
    self._config.save()

    if (obj["data"]["move_data_completed_mode"] == "subfolder" and
        self._prefs["options"]["move_on_changes"]):
      self._subtree_move_completed(label_id)


  @export
  @init_check
  def get_label_data(self, timestamp):

    if timestamp:
      t = cPickle.loads(timestamp)
    else:
      t = datetime.datetime(1, 1, 1)

    if t < self._last_modified:
      return (cPickle.dumps(self._last_modified), self._get_label_counts())
    else:
      return None


  @export
  @init_check
  @debug()
  def set_options(self, label_id, options_in):

    Validation.require(label_id not in RESERVED_IDS and
        label_id in self._labels, "Unknown Label")

    retroactive = options_in.get("tmp_auto_retroactive", False)
    unlabeled_only = options_in.get("tmp_auto_unlabeled", True)

    options = self._labels[label_id]["data"]

    old_download = options["download_settings"]
    old_move = options["move_data_completed"]
    old_move_path = options["move_data_completed_path"]

    self._normalize_label_data(options_in)
    options.update(options_in)

    self._config.save()

    for id in self._index[label_id]["torrents"]:
      self._apply_torrent_options(id)

    # Make sure descendent labels are updated if path changed
    if old_move_path != options["move_data_completed_path"]:
      self._propagate_path_to_descendents(label_id)

      self._config.save()

      if self._prefs["options"]["move_on_changes"]:
        self._subtree_move_completed(label_id)
    else:
      # If move completed was just turned on...
      if (options["download_settings"] and
          options["move_data_completed"] and
          (not old_download or not old_move) and
          self._prefs["options"]["move_on_changes"]):
        self._do_move_completed(label_id, self._index[label_id]["torrents"])

    if options["auto_settings"] and retroactive:
      autolabel = []
      for torrent_id in self._torrents:
        if not unlabeled_only or torrent_id not in self._mappings:
          if self._has_auto_apply_match(label_id, torrent_id):
            autolabel.append(torrent_id)

      if autolabel:
        self.set_torrent_labels(label_id, autolabel)

    self._last_modified = datetime.datetime.now()
    self._config.save()


  @export
  @init_check
  @debug()
  def get_options(self, label_id):

    Validation.require(label_id not in RESERVED_IDS and
        label_id in self._labels, "Unknown Label")

    return self._labels[label_id]["data"]


  @export
  @init_check
  @debug()
  def set_preferences(self, prefs):

    self._normalize_options(prefs["options"])
    self._prefs["options"].update(prefs["options"])

    self._normalize_label_data(prefs["defaults"])
    self._prefs["defaults"].update(prefs["defaults"])

    self._last_modified = datetime.datetime.now()
    self._config.save()


  @export
  @init_check
  @debug()
  def get_preferences(self):

    return self._prefs


  @export
  @init_check
  def get_parent_path(self, label_id):

    Validation.require(label_id not in RESERVED_IDS and
        label_id in self._labels, "Unknown Label")

    parent_id = Label.get_parent(label_id)
    if parent_id == NULL_PARENT:
      path = self._get_default_save_path()
    else:
      path = self._labels[parent_id]["data"]["move_data_completed_path"]

    return path


  @export
  @init_check
  @debug()
  def set_torrent_labels(self, label_id, torrent_list):

    Validation.require((label_id not in RESERVED_IDS and
        label_id in self._labels) or (not label_id), "Unknown Label")

    torrents = [t for t in torrent_list if t in self._torrents]
    for id in torrents:
      self._set_torrent_label(id, label_id)

    self._last_modified = datetime.datetime.now()
    self._config.save()

    self._do_move_completed(label_id, torrents)


  @export
  @init_check
  @debug()
  def get_torrent_label(self, torrent_id):

    return self._get_torrent_label(torrent_id)


  @export
  @init_check
  def get_daemon_vars(self):

    vars = {
      "os_path_module": os.path.__name__,
    }

    return vars


  @debug(show_args=True)
  def on_torrent_added(self, torrent_id):

    for label_id in self._labels:
      if label_id == NULL_PARENT: continue

      if self._labels[label_id]["data"]["auto_settings"]:
        if self._has_auto_apply_match(label_id, torrent_id):
          self._set_torrent_label(torrent_id, label_id)
          log.debug("[%s] Torrent %s is labeled %s", PLUGIN_NAME,
              torrent_id, label_id)

          self._config.save()

          break

    self._last_modified = datetime.datetime.now()


  @debug(show_args=True)
  def on_torrent_removed(self, torrent_id):

    if torrent_id in self._mappings:
      label_id = self._mappings[torrent_id]
      log.debug("[%s] Torrent %s is mapped to %s", PLUGIN_NAME,
          torrent_id, label_id)
      self._index[label_id]["torrents"].remove(torrent_id)
      del self._mappings[torrent_id]
      log.debug("[%s] Torrent removed from index and mappings", PLUGIN_NAME)

      self._config.save()

    self._last_modified = datetime.datetime.now()


  @debug()
  def on_torrent_finished(self, alert):

    torrent_id = str(alert.handle.info_hash())

    if torrent_id in self._mappings:
      log.debug("[%s] Labeled torrent %s finished", PLUGIN_NAME, torrent_id)
      label_id = self._mappings[torrent_id]
      torrent = self._torrents[torrent_id]

      path = torrent.get_status(["save_path"])["save_path"]
      if path != self._labels[label_id]["data"]["move_data_completed_path"]:
        self._do_move_completed(label_id, [torrent_id])


  def _initialize(self):

    component.get("EventManager").deregister_event_handler(
        "SessionStartedEvent", self._initialize)

    self._torrents = component.get("TorrentManager").torrents

    self._initialize_data()
    self._build_index()
    self._remove_orphans()

    component.get("FilterManager").register_filter(
        STATUS_ID, self._filter_by_label)

    component.get("CorePluginManager").register_status_field(
        STATUS_NAME, self._get_torrent_label_name)
    component.get("CorePluginManager").register_status_field(
        STATUS_ID, self._get_torrent_label)

    component.get("EventManager").register_event_handler(
        "TorrentAddedEvent", self.on_torrent_added)
    component.get("EventManager").register_event_handler(
        "PreTorrentRemovedEvent", self.on_torrent_removed)

    component.get("AlertManager").register_handler(
        "torrent_finished_alert", self.on_torrent_finished)

    self._last_modified = datetime.datetime.now()
    self.initialized = True

    log.debug("[%s] Core initialized", PLUGIN_NAME)


  def _initialize_data(self):

    for id in self._mappings.keys():
      if id not in self._torrents or self._mappings[id] not in self._labels:
        del self._mappings[id]

    for id in RESERVED_IDS:
      if id in self._labels:
        del self._labels[id]

    for id in self._labels:
      self._normalize_label_data(self._labels[id]["data"])

    self._labels[NULL_PARENT] = {
      "name": None,
      "data": None,
    }

    self._normalize_options(self._prefs["options"])
    self._normalize_label_data(self._prefs["defaults"])

    self._config.save()


  def _build_index(self):

    index = {}
    for id in self._labels:
      children = []
      torrents = []

      for child_id in self._labels:
        if child_id == id: continue

        if Label.get_parent(child_id) == id:
          children.append(child_id)

      for torrent_id in self._mappings:
        if self._mappings[torrent_id] == id:
          torrents.append(torrent_id)

      index[id] = {
        "children": children,
        "torrents": torrents,
      }

    self._index = index

    for id in self._labels:
      self._build_label_ancestry(id)


  def _remove_orphans(self):

    removals = []
    for id in self._labels:
      if id == NULL_PARENT: continue

      parent_id = Label.get_parent(id)
      if not self._labels.get(parent_id):
        removals.append(id)

    for id in removals:
      self._remove_label(id)


  def _filter_by_label(self, torrent_ids, label_ids):

    filtered = []

    for id in torrent_ids:
      label_id = self._mappings.get(id)

      if not label_id:
        if ID_NONE in label_ids:
          filtered.append(id)
      elif label_id in label_ids:
        filtered.append(id)
      elif self._prefs["options"]["include_children"]:
        if any(x for x in label_ids if Label.is_ancestor(x, label_id)):
          filtered.append(id)

    return filtered


  def _get_unused_id(self, parent_id):

    i = 0
    label_obj = {}
    while label_obj is not None:
      id = "%s:%s" % (parent_id, i)
      label_obj = self._labels.get(id)
      i += 1

    return id


  def _get_children_names(self, parent_id):

    names = []
    for id in self._index[parent_id]["children"]:
      names.append(self._labels[id]["name"])

    return names


  def _validate_name(self, parent_id, label_name):

    Validation.validate_name(label_name)

    names = self._get_children_names(parent_id)
    Validation.require(label_name not in names, "Label already exists")


  def _remove_label(self, label_id):

    for id in self._index[label_id]["children"]:
      self._remove_label(id)

    for id in self._index[label_id]["torrents"]:
      self._apply_torrent_options(id, reset=True)

      del self._mappings[id]

    del self._index[label_id]
    del self._labels[label_id]


  @debug(show_args=True)
  def _set_torrent_label(self, torrent_id, label_id):

    log.debug("[%s] Setting label %s on %s", PLUGIN_NAME,
        label_id, torrent_id)

    id = self._mappings.get(torrent_id)
    if id is not None:
      log.debug("[%s] Torrent current mapping: %s", PLUGIN_NAME, id)
      self._apply_torrent_options(torrent_id, reset=True)
      self._index[id]["torrents"].remove(torrent_id)
      del self._mappings[torrent_id]
      log.debug("[%s] Torrent removed from index and mappings", PLUGIN_NAME)

    if label_id:
      self._mappings[torrent_id] = label_id
      self._index[label_id]["torrents"].append(torrent_id)
      self._apply_torrent_options(torrent_id)
      log.debug("[%s] Torrent labeled %s and options applied",
          PLUGIN_NAME, label_id)


  def _get_label_counts(self):

    label_count = 0
    counts = {}
    for id in sorted(self._labels, reverse=True):
      if id == NULL_PARENT: continue

      count = len(self._index[id]["torrents"])
      label_count += count

      if self._prefs["options"]["include_children"]:
        for child in self._index[id]["children"]:
          count += counts[child]["count"]

      counts[id] = {
        "name": self._labels[id]["name"],
        "count": count,
      }

    total = len(self._torrents)
    counts[ID_ALL] = {
      "name": ID_ALL,
      "count": total,
    }

    counts[ID_NONE] = {
      "name": ID_NONE,
      "count": total-label_count,
    }

    return counts


  def _get_torrent_label(self, torrent_id):

    return self._mappings.get(torrent_id) or ""


  def _get_torrent_label_name(self, torrent_id):

    label_id = self._mappings.get(torrent_id)
    if not label_id:
      return ""

    if self._prefs["options"]["show_full_name"]:
      name = self._get_label_ancestry(label_id)
    else:
      name = self._labels[label_id]["name"]

    return name


  def _get_label_ancestry(self, label_id):

    ancestry_str = self._index[label_id].get("ancestry")
    if ancestry_str:
      return ancestry_str

    return self._build_label_ancestry(label_id)


  def _build_label_ancestry(self, label_id):

    members = []
    member = label_id
    while member and member != NULL_PARENT:
      ancestry_str = self._index[member].get("ancestry")
      if ancestry_str:
        members.append(ancestry_str)

        break

      members.append(self._labels[member]["name"])
      member = Label.get_parent(member)

    ancestry_str = "/".join(reversed(members))
    self._index[label_id]["ancestry"] = ancestry_str

    return ancestry_str


  def _clear_subtree_ancestry(self, parent_id):

    if self._index[parent_id].get("ancestry") is not None:
      del self._index[parent_id]["ancestry"]

    for id in self._index[parent_id]["children"]:
      self._clear_subtree_ancestry(id)


  def _has_auto_apply_match(self, label_id, torrent_id):

    name = self._torrents[torrent_id].get_status(["name"])["name"]
    trackers = tuple(t["url"] for t in self._torrents[torrent_id].trackers)

    options = self._labels[label_id]["data"]
    for line in options["auto_queries"]:
      terms = line.split()

      if options["auto_name"]:
        if all(t in name for t in terms):
          return True
      elif options["auto_tracker"]:
        for tracker in trackers:
          if all(t in tracker for t in terms):
            return True

    return False


  def _normalize_options(self, options):

    for key in options.keys():
      if key not in OPTION_DEFAULTS:
        del options[key]

    for key in OPTION_DEFAULTS:
      if key not in options:
        options[key] = OPTION_DEFAULTS[key]


  def _normalize_label_data(self, data):

    data["move_data_completed_path"] = \
        data["move_data_completed_path"].strip()
    if not data["move_data_completed_path"]:
      data["move_data_completed_path"] = self._get_default_save_path()

    queries = [line for line in data["auto_queries"] if line.strip()]
    data["auto_queries"] = queries

    for key in data.keys():
      if key not in LABEL_DEFAULTS:
        del data[key]

    for key in LABEL_DEFAULTS:
      if key not in data:
        data[key] = LABEL_DEFAULTS[key]


  def _apply_torrent_options(self, torrent_id, reset=False):

    label_id = self._mappings.get(torrent_id)

    options = self._labels[label_id]["data"]
    torrent = self._torrents[torrent_id]

    if not reset and options["download_settings"]:
      if options["move_data_completed"]:
        torrent.set_move_completed(options["move_data_completed"])
        torrent.set_move_completed_path(options["move_data_completed_path"])
      else:
        torrent.set_move_completed(self._core["move_completed"])
        torrent.set_move_completed_path(self._core["move_completed_path"])

      torrent.set_options({
        "prioritize_first_last_pieces": options["prioritize_first_last"],
      })
    else:
      torrent.set_move_completed(self._core["move_completed"])
      torrent.set_move_completed_path(self._core["move_completed_path"])
      torrent.set_options({
        "prioritize_first_last_pieces":
          self._core["prioritize_first_last_pieces"],
      })

    if not reset and options["bandwidth_settings"]:
      torrent.set_max_download_speed(options["max_download_speed"])
      torrent.set_max_upload_speed(options["max_upload_speed"])
      torrent.set_max_connections(options["max_connections"])
      torrent.set_max_upload_slots(options["max_upload_slots"])
    else:
      torrent.set_max_download_speed(
          self._core["max_download_speed_per_torrent"])
      torrent.set_max_upload_speed(self._core["max_upload_speed_per_torrent"])
      torrent.set_max_connections(self._core["max_connections_per_torrent"])
      torrent.set_max_upload_slots(self._core["max_upload_slots_per_torrent"])

    if not reset and options["queue_settings"]:
      torrent.set_auto_managed(options["auto_managed"])
      torrent.set_stop_at_ratio(options["stop_at_ratio"])
      torrent.set_stop_ratio(options["stop_ratio"])
      torrent.set_remove_at_ratio(options["remove_at_ratio"])
    else:
      torrent.set_auto_managed(self._core["auto_managed"])
      torrent.set_stop_at_ratio(self._core["stop_seed_at_ratio"])
      torrent.set_stop_ratio(self._core["stop_seed_ratio"])
      torrent.set_remove_at_ratio(self._core["remove_seed_at_ratio"])


  def _apply_data_completed_path(self, label_id):

    for id in self._index[label_id]["torrents"]:
      self._torrents[id].set_move_completed_path(
          self._labels[label_id]["data"]["move_data_completed_path"])


  def _propagate_path_to_descendents(self, parent_id):


    def descend(parent_id):
      name = self._labels[parent_id]["name"]
      options = self._labels[parent_id]["data"]

      mode = options["move_data_completed_mode"]
      if mode == "folder": return

      if mode == "subfolder":
        move_path.append(name)

      options["move_data_completed_path"] = os.path.join(*move_path)

      if options["download_settings"] and options["move_data_completed"]:
        self._apply_data_completed_path(parent_id)

      for id in self._index[parent_id]["children"]:
        descend(id)

      if mode == "subfolder":
        move_path.pop()


    options = self._labels[parent_id]["data"]
    path = options["move_data_completed_path"]

    move_path = [path]

    for id in self._index[parent_id]["children"]:
      descend(id)


  def _subtree_move_completed(self, parent_id):

    self._do_move_completed(parent_id, self._index[parent_id]["torrents"])

    for id in self._index[parent_id]["children"]:
      self._subtree_move_completed(id)


  def _do_move_completed(self, label_id, torrent_list):

    if label_id:
      options = self._labels[label_id]["data"]

    if (not label_id or (self._prefs["options"]["move_on_changes"] and
        options["download_settings"] and
        options["move_data_completed"])):
      try:
        component.get("CorePlugin.MoveTools").move_completed(torrent_list)
      except KeyError:
        pass


  def _get_default_save_path(self):

    path = self._core["download_location"]
    if not path:
      path = deluge.common.get_default_download_dir()

    return path


  def _rpc_deregister(self, name):

    server = component.get("RPCServer")
    name = name.lower()

    for d in dir(self):
      if d[0] == "_": continue

      if getattr(getattr(self, d), '_rpcserver_export', False):
        method = "%s.%s" % (name, d)
        log.debug("Deregistering method: %s", method)
        if method in server.factory.methods:
          del server.factory.methods[method]
