#===========================================================================
#
# Non-modem device all link database class
#
#===========================================================================
import functools
import io
import json
import os
from ..Address import Address
from .. import handler
from .DeviceEntry import DeviceEntry
from .. import log
from .. import message as Msg

LOG = log.get_logger()


class Device:
    """Device all link database.

    This class stores the all link database for an Insteon device.
    Each item is a DeviceEntry object that contains a single remote
    address, group, and type (controller vs responder).

    The database can be read to and written from JSOn format.
    Normally the db is constructed via message.InpExtended objects
    being read and parsed after requesting them from the device.

    Insteon devices use a "delta" to record the revision of the
    database on the device.  This class stores that as well so we know
    if the database is out of date with the one on the Insteon device.
    """

    @staticmethod
    def from_json(data, path):
        """Read a Device database from a JSON input.

        The inverse of this is to_json().

        Args:
          data:   (dict) The data to read from.
          path:   (str) The file to save the database to when changes are
                  made.

        Returns:
          Device: Returns the created Device object.
        """
        # Create the basic database object.
        obj = Device(Address(data['address']), path)

        # Extract the various files from the JSON data.
        obj.delta = data['delta']

        for d in data['used']:
            obj.add_entry(DeviceEntry.from_json(d))

        for d in data['unused']:
            obj.add_entry(DeviceEntry.from_json(d))

        return obj

    #-----------------------------------------------------------------------
    def __init__(self, addr, path=None):
        """Constructor

        Args:
          addr:  (Address) The Insteon address of the device the database
                 is for.
          path:  (str) The file to save the database to when changes are
                 made.
        """
        self.addr = addr
        self.save_path = path

        # All link delta number.  This is incremented by the device
        # when the db changes on the device.  It's returned in a
        # refresh (cmd=0x19) call to the device so we can check it
        # against the version we have stored.
        self.delta = None

        # Map of memory address (int) to DeviceEntry objects that are
        # active and in use.
        self.entries = {}

        # Map of memory address (int) to DeviceEntry objects that are
        # on the device but unused.  We need to keep these so we can
        # use these storage locations for future entries.
        self.unused = {}

        # Map of all link group number to DeviceEntry objects that
        # respond to that group command.
        self.groups = {}

        # Set of memory addresses that we have entries for.  This is
        # cleared when we start to download the db and used to filter
        # out duplicate entries.  Some devcies (smoke bridge) report a
        # lot of duplicate entries during db download for some reason.
        # This is the superset of addresses of self.entries and
        # self.unused.
        self._mem_locs = set()

        # Pending update function calls.  These are calls made to
        # add/del_on_device while another call is pending.  We can't
        # figure out what to do w/ the new call until the prev one
        # finishes and so we know the memory layout out the device.
        # These are function objects which are callable.
        self._pending = []

    #-----------------------------------------------------------------------
    def is_current(self, delta):
        """See if the database is current.

        The current delta is reported in the device status messages.
        Compare that against the stored delta in the database to see
        if this database is current.  If it's not, a new database
        needs to be downloaded from the device.

        Args:
          delta:  (int) The database delta to check

        Returns:
          (bool) Returns True if the database delta matches the input.
        """
        return delta == self.delta

    #-----------------------------------------------------------------------
    def set_delta(self, delta):
        """Set the current database delta.

        This records the input delta as the current value.  If the
        input isn't None, the database is also saved to record this
        value.

        Args:
          delta:  (int) The database delta.  None to clear the delta.
        """
        self.delta = delta
        if delta is not None:
            self.save()

    #-----------------------------------------------------------------------
    def clear(self):
        """Clear the complete database of entries.

        This also removes the saved file if it exists.  It does NOT
        modify the database on the device.
        """
        self.delta = None
        self.entries.clear()
        self.unused.clear()
        self.groups.clear()
        self._mem_locs.clear()

        if self.save_path and os.path.exists(self.save_path):
            os.remove(self.save_path)

    #-----------------------------------------------------------------------
    def set_path(self, path):
        """Set the save path to use for the database.

        Args:
          path:   (str) The file to save the database to when changes are
                  made.
        """
        self.save_path = path

    #-----------------------------------------------------------------------
    def save(self):
        """Save the database.

        If a save path wasn't set, nothing is done.
        """
        if not self.save_path:
            return

        with open(self.save_path, "w") as f:
            json.dump(self.to_json(), f, indent=2)

    #-----------------------------------------------------------------------
    def __len__(self):
        """Return the number of entries in the database.
        """
        return len(self.entries)

    #-----------------------------------------------------------------------
    def add_on_device(self, protocol, addr, group, is_controller, data,
                      on_done=None):
        """Add an entry and push the entry to the Insteon device.

        This sends the input record to the Insteon device.  If that
        command succeeds, it adds the new DeviceEntry record to the
        database and saves it.

        Multiple calls to this method are possible.  It will queue up
        pending calls until the previous calls complete (otherwise
        sending multiple calls before the message sequence is finished
        causes the device to abort the previous calls).

        The on_done callback will be passed a success flag
        (True/False), a string message about what happened, and the
        DeviceEntry that was created (if success=True).
            on_done( success, message, DeviceEntry )

        Args:
          protocol:      (Protocol) The Insteon protocol object to use for
                         sending messages.
          addr:          (Address) The address of the device in the database.
          group:         (int) The group the entry is for.
          is_controller: (bool) True if the device is a controller.
          data:          (bytes) 3 data bytes.  [0] is the on level, [1] is the
                         ramp rate.
          on_done:       Optional callback which will be called when the
                         command completes.
        """
        # Send the message write away.  Or if we're waiting for a
        # response, create a partial function with the inputs and add
        # it to the pending queue.
        if not self._pending:
            self._add_on_device(protocol, addr, group, is_controller, data,
                                on_done)
        else:
            LOG.info("Device %s busy - waiting to add to db", self.addr)
            func = functools.partial(self._add_on_device, protocol, addr,
                                     group, is_controller, data, on_done)
            self._pending.append(func)

    #-----------------------------------------------------------------------
    def delete_on_device(self, protocol, entry, on_done=None):
        """Delete an entry on the Insteon device.

        This sends the deletes the input record from the Insteon
        device.  If that command succeeds, it removes the DeviceEntry
        record to the database and saves it.

        Multiple calls to this method are possible.  It will queue up
        pending calls until the previous calls complete (otherwise
        sending multiple calls before the message sequence is finished
        causes the device to abort the previous calls).

        The on_done callback will be passed a success flag
        (True/False), a string message about what happened, and the
        DeviceEntry that was created (if success=True).
            on_done( success, message, DeviceEntry )

        Args:
          protocol:      (Protocol) The Insteon protocol object to use for
                         sending messages.
          entry:         (DeviceEntry) The entry to remove.
          on_done:       Optional callback which will be called when the
                         command completes.
        """
        # Send the message write away.  Or if we're waiting for a
        # response, create a partial function with the inputs and add
        # it to the pending queue.
        if not self._pending:
            self._delete_on_device(protocol, entry, on_done)
        else:
            LOG.info("Device %s busy - waiting to delete to db")
            func = functools.partial(self._delete_on_device, protocol, entry,
                                     on_done)
            self._pending.append(func)

    #-----------------------------------------------------------------------
    def find_group(self, group):
        """Find all the database entries in a group.

        Args:
          group:  (int) The group ID to find.

        Returns:
          [DeviceEntry] Returns a list of the database device entries that
          match the input group ID.
        """
        entries = self.groups.get(group, [])
        return entries

    #-----------------------------------------------------------------------
    def find(self, addr, group, is_controller):
        """Find an entry

        Args:
          addr:           (Address) The address to match.
          group:          (int) The group to match.
          is_controller:  (bool) True for controller records.  False for
                          responder records.

        Returns:
          (DeviceEntry): Returns the entry that matches or None if it
          doesn't exist.
        """
        # Convert to formal values - allows for string inputs for the
        # address for example.
        addr = Address(addr)
        group = int(group)

        for e in self.entries.values():
            if (e.addr == addr and e.group == group and
                    e.is_controller == is_controller):
                return e

        return None

    #-----------------------------------------------------------------------
    def find_mem_loc(self, mem_loc):
        """Find an entry by memory location.

        Args:
          mem_loc:  (int) The memory address to find.

        Returns:
          (DeviceEntry): Returns the entry or None if it doesn't exist.
        """
        return self.entries.get(mem_loc, None)

    #-----------------------------------------------------------------------
    def find_all(self, addr=None, group=None, is_controller=None):
        """Find all entries that match the inputs.

        Returns all the entries that match any input that is set.  If
        an input isn't set, that field isn't checked.

        Args:
          addr:           (Address) The address to match.  None for any.
          group:          (int) The group to match.  None for any.
          is_controller:  (bool) True for controller records.  False for
                          responder records.  None for any.

        Returns:
          [DeviceEntry] Returns a list of the entries that match.
        """
        addr = None if addr is None else Address(addr)
        group = None if group is None else int(group)

        results = []
        for e in self.entries.values():
            if addr is not None and e.addr != addr:
                continue
            if group is not None and e.group != group:
                continue
            if is_controller is not None and e.is_controller != is_controller:
                continue

            results.append(e)

        return results

    #-----------------------------------------------------------------------
    def to_json(self):
        """Convert the database to JSON format.

        Returns:
          (dict) Returns the database as a JSON dictionary.
        """
        used = [i.to_json() for i in self.entries.values()]
        unused = [i.to_json() for i in self.unused.values()]
        return {
            'address' : self.addr.to_json(),
            'delta' : self.delta,
            'used' : used,
            'unused' : unused,
            }

    #-----------------------------------------------------------------------
    def __str__(self):
        o = io.StringIO()
        o.write("DeviceDb: (delta %s)\n" % self.delta)

        # Sorting by address:
        #for elem in sorted(self.entries.values(), key=lambda i: i.addr.id):

        # Sorting by memory location
        for elem in sorted(self.entries.values(), key=lambda i: i.mem_loc):
            o.write("  %s\n" % elem)

        o.write("Unused:\n")
        for elem in sorted(self.unused.values(), key=lambda i: i.mem_loc):
            o.write("  %s\n" % elem)

        o.write("GroupMap\n")
        for grp, elem in self.groups.items():
            o.write("  %s -> %s\n" % (grp, [i.addr.hex for i in elem]))

        return o.getvalue()

    #-----------------------------------------------------------------------
    def add_entry(self, entry):
        """Add an entry to the database without updating the device.

        This is used when reading entries from disk.  It does NOT
        change the database on the Insteon device.

        Args:
          entry:  (DeviceEntry) The entry to add.
        """
        # Entry has a valid database entry
        if entry.db_flags.in_use:
            # NOTE: this relies on no-one keeping a handle to this
            # entry outside of this class.  This also handles
            # duplicate messages since they will have the same memory
            # location key.
            self.entries[entry.mem_loc] = entry
            self._mem_locs.add(entry.mem_loc)

            # If we're the controller for this entry, add it to the list
            # of entries for that group.
            if entry.db_flags.is_controller:
                responders = self.groups.setdefault(entry.group, [])
                if entry not in responders:
                    responders.append(entry)

        # Entry is not in use.
        else:
            # NOTE: this relies on no-one keeping a handle to this
            # entry outside of this class.  This also handles
            # duplicate messages since they will have the same memory
            # location key.
            self.unused[entry.mem_loc] = entry
            self._mem_locs.add(entry.mem_loc)

            # If the entry is a controller and it's in the group dict,
            # erase it from the group map.
            if entry.db_flags.is_controller and entry.group in self.groups:
                responders = self.groups[entry.group]
                for i in range(len(responders)):
                    if responders[i].addr == entry.addr:
                        del responders[i]
                        break

        # Save the updated database.
        self.save()

    #-----------------------------------------------------------------------
    def _add_on_device(self, protocol, addr, group, is_controller, data,
                       on_done):
        """Add an entry on the remote device.

        See add_on_device() for docs.
        """
        # Insure types are ok - this way strings passed in from JSON
        # or MQTT get converted to the type we expect.
        addr = Address(addr)
        group = int(group)
        data = data if data else bytes(3)

        # If the record already exists, don't do anything.
        entry = self.find(addr, group, is_controller)
        if entry:
            # TODO: support checking and updating data
            LOG.warning("Device %s add db already exists for %s grp %s %s",
                        self.addr, addr, group,
                        'CTRL' if is_controller else 'RESP')
            if on_done:
                on_done(True, "Entry already exists", entry)
            return

        LOG.info("Device %s adding db: %s grp %s %s %s", self.addr, addr,
                 group, 'CTRL' if is_controller else 'RESP', data)
        assert len(self.entries)

        # When complete, remove this call from the pending list.  Then
        # call the user input callback if supplied, and call the next
        # pending call if one is waiting.
        def done_cb(success, msg, entry):
            LOG.debug("add_on_device done_cb %s", len(self._pending))
            self._pending.pop(0)
            if self._pending:
                LOG.debug("add_on_device calling next")
                self._pending[0]()
            elif on_done:
                on_done(success, msg, entry)

        # If there are entries in the db that are mark unused, we can
        # re-use those memory addresses and just update them w/ the
        # correct information and mark them as used.
        if self.unused:
            self._add_using_unused(protocol, addr, group, is_controller, data,
                                   done_cb)

        # If there no unused entries, we need to append one.  Write a
        # new record at the next memory location below the current
        # last entry and mark that as the new last entry.  If that
        # works, then update the record before it (the old last entry)
        # and mark it as not being the last entry anymore.  This order
        # is important since if either operation fails, the db is
        # still in a valid order.
        else:
            self._add_using_new(protocol, addr, group, is_controller, data,
                                done_cb)

        # Push a dummy pending entry to the list on the first call.
        # This way cb() has something to remove and future calls to
        # this know that there is a call in progress.
        if not self._pending:
            self._pending.append(True)

    #-----------------------------------------------------------------------
    def _delete_on_device(self, protocol, entry, on_done):
        """Delete an entry on the remote device.

        See delete_on_device() for docs.
        """
        # see p117 of insteon dev guide: To delete a record, set the
        # in use flag in DbFlags to 0.

        # When complete, remove this call from the pending list.  Then
        # call the user input callback if supplied, and call the next
        # pending call if one is waiting.
        def done_cb(success, msg, entry):
            self._pending.pop(0)
            if self._pending:
                self._pending[0]()
            elif on_done:
                on_done(success, msg, entry)

        # Copy the entry and mark it as unused.
        new_entry = entry.copy()
        new_entry.db_flags.in_use = False

        # Build the extended db modification message.  This says to
        # modify the entry in place w/ the new db flags which say this
        # record is no longer in use.
        ext_data = new_entry.to_bytes()
        msg = Msg.OutExtended.direct(self.addr, 0x2f, 0x00, ext_data)
        msg_handler = handler.DeviceDbModify(self, new_entry, done_cb)

        # Send the message.
        protocol.send(msg, msg_handler)

        # Push a dummy pending entry to the list on the first call.
        # This way cb() has something to remove and future calls to
        # this know that there is a call in progress.
        if not self._pending:
            self._pending.append(True)

    #-----------------------------------------------------------------------
    def _add_using_unused(self, protocol, addr, group, is_controller, data,
                          on_done):
        """Add an entry using an existing, unused entry.

        Grabs the first entry w/ the used flag=False and tells the
        device to update that record.
        """
        # Grab the first unused entry (highest memory address).
        entry = self.unused.pop(max(self.unused.keys()))
        LOG.info("Device %s using unused entry at mem %#06x", self.addr,
                 entry.mem_loc)

        # Update it w/ the new information.
        entry.update_from(addr, group, is_controller, data)

        # Build the extended db modification message.  This says to
        # update the record at the entry memory location.
        ext_data = entry.to_bytes()
        msg = Msg.OutExtended.direct(self.addr, 0x2f, 0x00, ext_data)
        msg_handler = handler.DeviceDbModify(self, entry, on_done)

        # Send the message and handler.
        protocol.send(msg, msg_handler)

    #-----------------------------------------------------------------------
    def _add_using_new(self, protocol, addr, group, is_controller, data,
                       on_done):
        """Add a anew entry at the end of the database.

        First we send the new entry to the remote device.  If that works,
        then we update the previously last entry to mart it as "not the last
        entry" so the device knows there is one more record.
        """
        # pylint: disable=too-many-locals

        # Memory goes high->low so find the last entry by looking at the
        # minimum value.  Then find the entry for that loc.
        last_entry = self.find_mem_loc(min(self._mem_locs))
        # TODO???
        if not last_entry:
            LOG.error("MEM_LOCS error: %s", self._mem_locs)
            return

        # Each rec is 8 bytes so move down 8 to get the next loc.
        mem_loc = last_entry.mem_loc - 0x08
        LOG.info("Device %s appending new record at mem %#06x", self.addr,
                 mem_loc)

        # Create the new entry and send it out.
        db_flags = Msg.DbFlags(in_use=True, is_controller=is_controller,
                               is_last_rec=True)
        entry = DeviceEntry(addr, group, mem_loc, db_flags, data)
        ext_data = entry.to_bytes()
        msg = Msg.OutExtended.direct(self.addr, 0x2f, 0x00, ext_data)
        msg_handler = handler.DeviceDbModify(self, entry, on_done)

        # Now create the updated current last entry w/ the last record flag
        # set to False since it's not longer last.  The handler will send
        # this message out if the first call above gets an ACK.
        new_last = last_entry.copy()
        new_last.db_flags.is_last_rec = False
        ext_data = new_last.to_bytes()
        next_msg = Msg.OutExtended.direct(self.addr, 0x2f, 0x00, ext_data)
        msg_handler.add_update(next_msg, new_last)

        # Send the message and handler.
        protocol.send(msg, msg_handler)

    #-----------------------------------------------------------------------
