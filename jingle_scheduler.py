"""

Features:
 - Use local audio files (add songs and jingles)
 - Basic playlist management (add, remove, reorder)
 - Play/pause/next
 - Two scheduling modes for jingles:
    1) Specific clock times (add times like 14:30, will play the jingle at the next song break after that time)
    2) N times per hour (distributes jingles roughly evenly, played between songs)
 - Jingles are always played between songs (app will wait for current song to finish, then play jingle if schedule calls for it)

How scheduling works (simple, robust approach):
 - The app tracks "next_jingle_time" when using "per-hour" mode: next_jingle_time = last_jingle_time + 3600/N
 - It also stores a list of absolute times for time-based jingles.
 - After a song finishes, the player checks whether a jingle should be played now (current time >= next_jingle_time or passed a scheduled clock-time that hasn't been fired yet).
 - If yes, it plays a randomly-chosen jingle (or the selected jingle) before continuing the main playlist.

"""

import sys
import time
import threading
import random
from datetime import datetime, timedelta
from PyQt5 import QtWidgets, QtCore
import vlc
import json
from pathlib import Path
from PyQt5.QtWidgets import QShortcut
from PyQt5.QtGui import QKeySequence, QKeyEvent


CACHE_FILE = str(Path.home() / '.jingle_scheduler_cache.json')


class SchedulerState:
    def __init__(self):
        self.mode = None  # 'times' or 'per_hour' or None
        self.times = []  # list of time strings 'HH:MM'
        self.per_hour = 0
        self.next_jingle_time = None
        self.last_jingle_time = None

    def set_per_hour(self, n):
        self.mode = 'per_hour' if n > 0 else None
        self.per_hour = n
        now = datetime.now()
        self.last_jingle_time = now
        if n > 0:
            self.next_jingle_time = now + timedelta(seconds=3600.0 / n)
        else:
            self.next_jingle_time = None

    def add_time(self, timestr):
        try:
            t = datetime.strptime(timestr, '%H:%M').time()
        except ValueError:
            return False
        self.times.append(timestr)
        self.mode = 'times'
        return True

    def remove_time(self, timestr):
        if timestr in self.times:
            self.times.remove(timestr)
        if not self.times and self.per_hour == 0:
            self.mode = None

    def should_play_jingle_after_song(self):
        now = datetime.now()
        # 1) Check absolute times (mode == 'times')
        if 'times' == self.mode and self.times:
            # if any scheduled time <= now and not fired (we'll treat as fired by removing after playback)
            for tstr in list(self.times):
                t = datetime.strptime(tstr, '%H:%M').time()
                candidate = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                # if candidate is in future but within 59 seconds, allow next break to play it
                if candidate <= now:
                    # it's due
                    self.times.remove(tstr)
                    # keep mode if more times left
                    if not self.times and self.per_hour == 0:
                        self.mode = None
                    return True
        # 2) per-hour
        if 'per_hour' == self.mode and self.per_hour > 0:
            if self.next_jingle_time is None:
                return False
            if now >= self.next_jingle_time:
                # advance next_jingle_time forward until in future
                while now >= self.next_jingle_time:
                    self.last_jingle_time = self.next_jingle_time
                    self.next_jingle_time = self.next_jingle_time + timedelta(seconds=3600.0 / self.per_hour)
                return True
        return False


class JingleSchedulerApp(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Jingle Scheduler')
        self.setMinimumSize(1000, 600)
        self.resize(1000, 600)

        # VLC player
        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()
        self.jplayer = None  # for playing jingles separately

        # Data
        self.playlist = []  # list of paths (songs)
        self.jingles = []  # list of paths (jingles)
        self.play_index = 0
        self.scheduler = SchedulerState()
        self.playing_jingle = False

        # UI
        self.playlistView = QtWidgets.QListWidget()
        self.jingleView = QtWidgets.QListWidget()
        self.playlistView.installEventFilter(self)
        self.jingleView.installEventFilter(self)
        self.timeList = QtWidgets.QListWidget()
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QHBoxLayout(central)
        self.playlistView.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

        # --- Playlist Delete Shortcuts ---
        self.shortcut_delete_playlist = QShortcut(QKeySequence(QtCore.Qt.Key_Delete), self.playlistView)
        self.shortcut_delete_playlist.activated.connect(self.remove_selected_songs)

        self.shortcut_backspace_playlist = QShortcut(QKeySequence(QtCore.Qt.Key_Backspace), self.playlistView)
        self.shortcut_backspace_playlist.activated.connect(self.remove_selected_songs)

        # --- Jingle Delete Shortcuts ---
        self.shortcut_delete_jingle = QShortcut(QKeySequence(QtCore.Qt.Key_Delete), self.jingleView)
        self.shortcut_delete_jingle.activated.connect(self.remove_selected_jingles)

        self.shortcut_backspace_jingle = QShortcut(QKeySequence(QtCore.Qt.Key_Backspace), self.jingleView)
        self.shortcut_backspace_jingle.activated.connect(self.remove_selected_jingles)

        left = QtWidgets.QVBoxLayout()
        right = QtWidgets.QVBoxLayout()
        layout.addLayout(left, 3)
        layout.addLayout(right, 1)

        self.playlistView.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.jingleView.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

        self.playlistView.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.jingleView.setFocusPolicy(QtCore.Qt.StrongFocus)

        # --- Playlist Delete Shortcut ---
        self.shortcut_playlist_delete = QShortcut(QKeySequence("Delete"), self.playlistView)
        self.shortcut_playlist_delete.activated.connect(self.remove_selected_songs)

        self.shortcut_playlist_backspace = QShortcut(QKeySequence("Backspace"), self.playlistView)
        self.shortcut_playlist_backspace.activated.connect(self.remove_selected_songs)

        # --- Jingle Delete Shortcut ---
        self.shortcut_jingle_delete = QShortcut(QKeySequence("Delete"), self.jingleView)
        self.shortcut_jingle_delete.activated.connect(self.remove_selected_jingles)

        self.shortcut_jingle_backspace = QShortcut(QKeySequence("Backspace"), self.jingleView)
        self.shortcut_jingle_backspace.activated.connect(self.remove_selected_jingles)

        # Load cached playlist and jingles
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
                self.playlist = cache.get('playlist', [])
                self.jingles = cache.get('jingles', [])
                for p in self.playlist:
                    self.playlistView.addItem(p)
                for j in self.jingles:
                    self.jingleView.addItem(j)
                # restore scheduler settings
                self.scheduler.set_per_hour(cache.get('per_hour', 0))
                for t in cache.get('scheduled_times', []):
                    self.scheduler.add_time(t)
                    self.timeList.addItem(t)
        except FileNotFoundError:
            pass
        except Exception as e:
            print("Failed to load cache:", e)

        # Playlist view
        left.addWidget(QtWidgets.QLabel('Playlist (songs):'))
        left.addWidget(self.playlistView)

        btnRow = QtWidgets.QHBoxLayout()
        left.addLayout(btnRow)
        addSongBtn = QtWidgets.QPushButton('Add Songs')
        addSongBtn.clicked.connect(self.add_songs)
        btnRow.addWidget(addSongBtn)
        removeSongBtn = QtWidgets.QPushButton('Remove')
        removeSongBtn.clicked.connect(self.remove_selected_songs)
        btnRow.addWidget(removeSongBtn)
        upBtn = QtWidgets.QPushButton('Move Up')
        upBtn.clicked.connect(self.move_up)
        btnRow.addWidget(upBtn)
        downBtn = QtWidgets.QPushButton('Move Down')
        downBtn.clicked.connect(self.move_down)
        btnRow.addWidget(downBtn)

        # Jingle section
        left.addWidget(QtWidgets.QLabel('Jingles (played between songs):'))
        left.addWidget(self.jingleView)
        jingBtnRow = QtWidgets.QHBoxLayout()
        left.addLayout(jingBtnRow)
        addJingleBtn = QtWidgets.QPushButton('Add Jingles')
        addJingleBtn.clicked.connect(self.add_jingles)
        jingBtnRow.addWidget(addJingleBtn)
        removeJingleBtn = QtWidgets.QPushButton('Remove Selected')
        removeJingleBtn.clicked.connect(self.remove_selected_jingles)
        jingBtnRow.addWidget(removeJingleBtn)

        # Player controls
        controls = QtWidgets.QHBoxLayout()
        left.addLayout(controls)
        self.playBtn = QtWidgets.QPushButton('Play')
        self.playBtn.clicked.connect(self.play_pause)
        controls.addWidget(self.playBtn)
        self.nextBtn = QtWidgets.QPushButton('Next')
        self.nextBtn.clicked.connect(self.play_next)
        controls.addWidget(self.nextBtn)
        self.stopBtn = QtWidgets.QPushButton('Stop')
        self.stopBtn.clicked.connect(self.stop)
        controls.addWidget(self.stopBtn)

        # Right: scheduler and status
        right.addWidget(QtWidgets.QLabel('Scheduler'))
        form = QtWidgets.QFormLayout()
        right.addLayout(form)

        self.perHourSpin = QtWidgets.QSpinBox()
        self.perHourSpin.setRange(0, 60)
        self.perHourSpin.setValue(0)
        form.addRow('Times per hour (0 to disable):', self.perHourSpin)
        setPerHourBtn = QtWidgets.QPushButton('Set')
        setPerHourBtn.clicked.connect(self.set_per_hour)
        form.addRow('', setPerHourBtn)

        self.timeEdit = QtWidgets.QLineEdit()
        self.timeEdit.setPlaceholderText('HH:MM')
        addTimeBtn = QtWidgets.QPushButton('Add Clock Time')
        addTimeBtn.clicked.connect(self.add_clock_time)
        form.addRow(self.timeEdit, addTimeBtn)

        right.addWidget(QtWidgets.QLabel('Scheduled clock times (will be played at next song-break after the time passes):'))
        right.addWidget(self.timeList)

        # Status
        right.addWidget(QtWidgets.QLabel('Status:'))
        self.statusLabel = QtWidgets.QLabel('Stopped')
        right.addWidget(self.statusLabel)

        self.playlistView.setFocus()

        # Start monitor thread
        self.monitor_thread = threading.Thread(target=self._monitor_playback, daemon=True)
        self._monitor_stop = False
        self.monitor_thread.start()

    # UI actions
    def add_songs(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, 'Select songs', '', 'Audio Files (*.mp3 *.wav *.ogg *.flac);;All Files (*)')
        for f in files:
            self.playlist.append(f)
            self.playlistView.addItem(f)

    def remove_selected_songs(self):
        items = self.playlistView.selectedIndexes()
        if not items:
            return

        rows = sorted([i.row() for i in items], reverse=True)

        for row in rows:
            self.playlistView.takeItem(row)
            del self.playlist[row]

    # def remove_selected_songs(self):
    #     for item in self.playlistView.selectedItems():
    #         row = self.playlistView.row(item)
    #         self.playlistView.takeItem(row)
    #         del self.playlist[row]

    def remove_selected_jingles(self):
        for item in self.jingleView.selectedItems():
            row = self.jingleView.row(item)
            self.jingleView.takeItem(row)
            del self.jingles[row]

    def move_up(self):
        items = self.playlistView.selectedIndexes()
        if not items:
            return

        rows = sorted([i.row() for i in items])

        if rows[0] == 0:
            return  # already at top

        # Move all up
        for row in rows:
            item = self.playlistView.takeItem(row)
            self.playlistView.insertItem(row - 1, item)

            self.playlist[row], self.playlist[row - 1] = (
                self.playlist[row - 1],
                self.playlist[row],
            )

        # Reselect items at new positions
        self.playlistView.clearSelection()
        for row in [r - 1 for r in rows]:
            self.playlistView.item(row).setSelected(True)

    def move_down(self):
        items = self.playlistView.selectedIndexes()
        if not items:
            return

        rows = sorted([i.row() for i in items], reverse=True)

        if rows[0] == self.playlistView.count() - 1:
            return  # already at bottom

        # Move all down
        for row in rows:
            item = self.playlistView.takeItem(row)
            self.playlistView.insertItem(row + 1, item)

            self.playlist[row], self.playlist[row + 1] = (
                self.playlist[row + 1],
                self.playlist[row],
            )

        # Reselect items at new positions
        self.playlistView.clearSelection()
        for row in [r + 1 for r in rows]:
            self.playlistView.item(row).setSelected(True)

    # def move_up(self):
    #     sel = self.playlistView.currentRow()
    #     if sel > 0:
    #         self.playlist[sel-1], self.playlist[sel] = self.playlist[sel], self.playlist[sel-1]
    #         item = self.playlistView.takeItem(sel)
    #         self.playlistView.insertItem(sel-1, item)
    #         self.playlistView.setCurrentRow(sel-1)
    #
    # def move_down(self):
    #     sel = self.playlistView.currentRow()
    #     if self.playlistView.count()-1 > sel >= 0:
    #         self.playlist[sel+1], self.playlist[sel] = self.playlist[sel], self.playlist[sel+1]
    #         item = self.playlistView.takeItem(sel)
    #         self.playlistView.insertItem(sel+1, item)
    #         self.playlistView.setCurrentRow(sel+1)

    def add_jingles(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, 'Select jingles', '', 'Audio Files (*.mp3 *.wav *.ogg *.flac);;All Files (*)')
        for f in files:
            self.jingles.append(f)
            self.jingleView.addItem(f)

    def set_per_hour(self):
        n = int(self.perHourSpin.value())
        self.scheduler.set_per_hour(n)
        self.update_status()

    def add_clock_time(self):
        t = self.timeEdit.text().strip()
        ok = self.scheduler.add_time(t)
        if not ok:
            QtWidgets.QMessageBox.warning(self, 'Bad time', 'Please enter time in HH:MM format')
            return
        self.timeList.addItem(t)
        self.timeEdit.clear()
        self.update_status()


    # Listeners
    # def eventFilter(self, source, event):
    #     if event.type() == QtCore.QEvent.KeyPress:
    #         if event.key() in (QtCore.Qt.Key_Delete, QtCore.Qt.Key_Backspace):
    #             if source is self.playlistView:
    #                 self.remove_selected_songs()
    #                 return True
    #             elif source is self.jingleView:
    #                 self.remove_selected_jingles()
    #                 return True
    #     return super().eventFilter(source, event)

    def eventFilter(self, source, event):
        if isinstance(event, QKeyEvent) and event.type() == QtCore.QEvent.KeyPress:
            print(f"key: {event.text()}")
            if event.key() in (QtCore.Qt.Key_Delete, QtCore.Qt.Key_Backspace, 82):
                if source is self.playlistView:
                    self.remove_selected_songs()
                    return True
                elif source is self.jingleView:
                    self.remove_selected_jingles()
                    return True
        return super().eventFilter(source, event)

    # Playback control
    def play_pause(self):
        if self.player.is_playing():
            self.player.pause()
            self.playBtn.setText('Play')
            self.update_status()
            return
        # if stopped or paused, start
        if not self.playlist:
            QtWidgets.QMessageBox.warning(self, 'No songs', 'Please add songs to playlist')
            return
        if self.player.get_state() == vlc.State.Ended or self.player.get_state() == vlc.State.NothingSpecial:
            # fresh start
            self.play_index = max(0, min(self.play_index, len(self.playlist)-1))
            self._play_current_song()
        else:
            self.player.play()
        self.playBtn.setText('Pause')
        self.update_status()

    def stop(self):
        if self.jplayer and self.playing_jingle:
            self.playing_jingle = False
            self.jplayer.stop()
            self.jplayer = None
        self.player.stop()
        self.playBtn.setText('Play')
        self.update_status()

    def play_next(self):
        # Stop any active jingle
        if self.jplayer and self.playing_jingle:
            self.playing_jingle = False
            self.jplayer.stop()
            self.jplayer = None
        self._advance_index()
        self._play_current_song()

    def _advance_index(self):
        if not self.playlist:
            return
        self.play_index = (self.play_index + 1) % len(self.playlist)
        self.playlistView.setCurrentRow(self.play_index)

    def _play_current_song(self):
        if not self.playlist:
            return
        path = self.playlist[self.play_index]
        media = self.instance.media_new(path)
        self.player.set_media(media)
        self.player.play()
        self.playBtn.setText('Pause')
        self.statusLabel.setText(f'Playing: {Path(path).name}')

    def _play_jingle_once(self, jingle_path):
        if not jingle_path:
            return
        self.playing_jingle = True
        self.statusLabel.setText(f'Playing jingle: {Path(jingle_path).name}')
        self.jplayer = self.instance.media_player_new()
        jmedia = self.instance.media_new(jingle_path)
        self.jplayer.set_media(jmedia)
        self.jplayer.play()

        # Wait until finished or until externally stopped
        while self.playing_jingle:
            state = self.jplayer.get_state()
            if state in (vlc.State.Ended, vlc.State.Stopped, vlc.State.Error):
                break
            time.sleep(0.2)
        self.playing_jingle = False
        self.jplayer = None

    # Monitor playback thread: when a song ends, decide whether to play a jingle and advance
    def _monitor_playback(self):
        while not self._monitor_stop:
            try:
                state = self.player.get_state()
                # If song ended (or nothing playing, but we were previously playing) -> handle next
                if state == vlc.State.Ended:
                    # finished a song
                    # check scheduler
                    if self.scheduler.should_play_jingle_after_song() and self.jingles:
                        # pick jingle (random for now)
                        jingle_path = random.choice(self.jingles)
                        self._play_jingle_once(jingle_path)
                    # advance and play next song
                    self._advance_index()
                    self._play_current_song()
                # update status occasionally
                time.sleep(0.5)
            except vlc.VLCException:
                time.sleep(0.5)

    def closeEvent(self, event):
        # Stop all players
        self._monitor_stop = True
        try:
            self.player.stop()
            if self.jplayer and self.playing_jingle:
                self.jplayer.stop()
        except vlc.VLCException:
            pass

        # Save playlist and jingles to cache
        cache = {
            'playlist': self.playlist,
            'jingles': self.jingles,
            'per_hour': self.scheduler.per_hour,
            'scheduled_times': self.scheduler.times
        }
        try:
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache, f, indent=2)
        except Exception as e:
            print("Failed to save cache:", e)

        event.accept()

    def update_status(self):
        lines = []
        if self.player.is_playing():
            lines.append('Playing')
        else:
            lines.append('Stopped/Paused')
        if self.scheduler.mode == 'per_hour' and self.scheduler.per_hour > 0:
            lines.append(f'{self.scheduler.per_hour} jingles/hour; next approx at {self.scheduler.next_jingle_time.strftime("%H:%M:%S") if self.scheduler.next_jingle_time else "N/A"}')
        if self.scheduler.mode == 'times' and self.scheduler.times:
            lines.append('Clock times queued: ' + ', '.join(self.scheduler.times))
        self.statusLabel.setText(' | '.join(lines))


if __name__ == '__main__':
    # Enable automatic scaling based on system DPI
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps)

    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")

    # palette = QPalette()
    # palette.setColor(QPalette.Window, QColor(40, 40, 40))
    # palette.setColor(QPalette.WindowText, QtCore.Qt.white)
    # palette.setColor(QPalette.Base, QColor(30, 30, 30))
    # palette.setColor(QPalette.AlternateBase, QColor(45, 45, 45))
    # palette.setColor(QPalette.ToolTipBase, QtCore.Qt.white)
    # palette.setColor(QPalette.ToolTipText, QtCore.Qt.white)
    # palette.setColor(QPalette.Text, QtCore.Qt.white)
    # palette.setColor(QPalette.Button, QColor(60, 60, 60))
    # palette.setColor(QPalette.ButtonText, QtCore.Qt.white)
    # palette.setColor(QPalette.BrightText, QtCore.Qt.red)
    # palette.setColor(QPalette.Highlight, QColor(255, 99, 71))  # tomato red
    # palette.setColor(QPalette.HighlightedText, QtCore.Qt.black)
    #
    # app.setPalette(palette)

    app.setStyleSheet("""
           QWidget {
               background-color: #2E2E2E;     /* warm dark gray */
               color: #F2F2F2;
               font-family: 'Segoe UI', sans-serif;
               font-size: 11pt;
           }

           QPushButton {
               background-color: #D4A056;     /* soft amber / gold */
               border: none;
               padding: 8px 14px;
               border-radius: 8px;
               color: #2E2E2E;
               font-weight: bold;
           }
           QPushButton:hover {
               background-color: #E0B46C;     /* lighter amber */
           }
           QPushButton:pressed {
               background-color: #B88743;     /* deeper amber */
           }

           QListWidget {
               background-color: #1F1F1F;     /* near-black warm gray */
               border: 1px solid #555;
               border-radius: 8px;
               padding: 6px;
               color: #F2F2F2;
               selection-background-color: #E0B46C;
               selection-color: #2E2E2E;
           }

           QLabel {
               font-weight: bold;
               color: #FFD37A;                /* light amber highlight */
           }

           QMenuBar, QMenu {
               background-color: #1F1F1F;
               color: #F2F2F2;
               border: none;
           }
           QMenu::item:selected {
               background-color: #D4A056;
               color: #2E2E2E;
           }

           QScrollBar:vertical {
               background-color: #121212;
               width: 10px;
               border-radius: 4px;
           }
           QScrollBar::handle:vertical {
               background-color: #D4A056;     /* amber */
               border-radius: 4px;
               min-height: 20px;
           }
           QScrollBar::handle:vertical:hover {
               background-color: #E0B46C;
           }

           QSpinBox {
               background-color: #1F1F1F;
               color: #F2F2F2;
               border: 1px solid #555;
               border-radius: 6px;
               padding-right: 24px;
               padding-left: 6px;
               selection-background-color: #E0B46C;
               selection-color: #2E2E2E;
           }            

           QSpinBox:hover {
               background-color: #262626;
           }

           QSpinBox::up-button, QSpinBox::down-button {
               background-color: #D4A056;
               border: none;
               subcontrol-origin: border;
               width: 18px;
               border-radius: 3px;
           }

           QSpinBox::up-button:hover, QSpinBox::down-button:hover {
               background-color: #E0B46C;
           }
    """)

    font = app.font()
    font.setPointSize(int(font.pointSize() * 2))
    app.setFont(font)

    w = JingleSchedulerApp()
    w.show()
    sys.exit(app.exec_())
