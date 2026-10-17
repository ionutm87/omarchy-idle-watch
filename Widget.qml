import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Bar glyph: green check while all is well, warning when check.py reports something,
// red for urgent faults. Click opens a panel listing only problems: hung processes,
// broken services, crashes, zombies, stuck I/O, full disks and apps on stale binaries.
// Clicking a process opens a details column beside the list, with red flags and stop.
// Process names are arbitrary strings, so every Text is PlainText.
Panel {
  id: root
  moduleName: "ionutm87.idle-watch"
  ipcTarget: "ionutm87.idle-watch"

  property var issues: []
  property var termed: ({})       // "pid:start" -> true once TERM was sent
  property string status: ""
  property var selected: null     // {pid, start} of the row whose details are shown
  property var details: null
  property var stale: []          // groups of processes running a deleted/replaced binary
  property string staleGroup: ""  // key (package, or file) of the group shown on its own, "" for the normal view
  property string pending: ""     // "stop:<key>" / "restart:<key>" waiting for the inline confirm in its row
  property string busy: ""        // key of the group whose stop/restart is running
  property var staleKeys: []      // group keys only, so the rows aren't rebuilt (and clicks lost) on every refresh
  property var faults: []         // broken services, crashes, OOM kills, zombies, stuck I/O, full disks
  property var faultKeys: []      // keys of issues + faults, same reason as staleKeys
  property var hidden: []         // keys acked here: filtered at once, so a cached or in-flight check can't bring them back
  readonly property bool urgent: root.issues.length > 0 || root.faults.some(f => f.urgent)
  readonly property bool allOk: root.faultKeys.length === 0 && root.staleKeys.length === 0
  readonly property color okColor: "#6abf69"
  readonly property string script: String(Qt.resolvedUrl("check.py")).replace(/^file:\/\//, "")

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function plural(n, one, many) { return n + " " + (n === 1 ? one : (many || one + "s")) }

  function check() {
    if (checkProc.running) return
    checkProc.command = ["python3", root.script,
      "--idle", idle.isIdle ? "1" : "0",
      "--cpu", String(root.setting("cpuPercent", 50)),
      "--gpu", String(root.setting("gpuPercent", 30)),
      "--temp", String(root.setting("tempC", 85)),
      "--hung-minutes", String(root.setting("hungMinutes", 10))]
    checkProc.running = true
  }

  function refreshTop() {
    if (!staleProc.running) staleProc.running = true
    if (!faultsProc.running) faultsProc.running = true
  }

  function toggleGroup(g) {
    root.staleGroup = root.staleGroup === g.key ? "" : g.key
  }

  function setFaults(issues, faults) {
    issues = issues.filter(f => root.hidden.indexOf(f.key) < 0)
    faults = faults.filter(f => root.hidden.indexOf(f.key) < 0)
    root.issues = issues
    root.faults = faults
    var keys = issues.concat(faults).map(f => f.key)
    if (JSON.stringify(keys) !== JSON.stringify(root.faultKeys)) root.faultKeys = keys
  }

  function faultFor(key) {
    var all = root.issues.concat(root.faults)
    for (var i = 0; i < all.length; i++) if (all[i].key === key) return all[i]
    return null
  }

  // "ack" only hides the row, so it runs at once (never dropped, even mid-fix); service actions ask inline first.
  function fixFault(f, action) {
    root.pending = ""
    if (!f || !f.key) return
    if (action === "ack") {
      root.hidden = root.hidden.concat([f.key])
      root.setFaults(root.issues, root.faults)
      Quickshell.execDetached({ command: ["python3", root.script, "--fix", f.key, "--action", "ack"] })
      return
    }
    if (fixProc.running) return
    root.busy = f.key
    fixProc.command = ["python3", root.script, "--fix", f.key, "--action", action]
    fixProc.running = true
  }

  // Opens Claude Code in a new terminal, already briefed on the problem; it asks before acting.
  function askClaude(prompt) {
    if (!prompt) return
    Quickshell.execDetached({
      command: ["/usr/share/omarchy/bin/omarchy-launch-tui", "--app-id=org.omarchy.claude-fix",
                Quickshell.env("HOME") + "/.local/bin/claude", prompt],
      workingDirectory: Quickshell.env("HOME")
    })
    root.close()
  }

  function groupFor(path) {
    for (var i = 0; i < root.stale.length; i++) if (root.stale[i].key === path) return root.stale[i]
    return null
  }

  function select(row) {
    if (root.selected && root.selected.pid === row.pid && root.selected.start === row.start) {
      root.selected = null
      root.details = null
      return
    }
    root.selected = { pid: row.pid, start: row.start }
    root.details = null
    refreshDetails()
  }

  function refreshDetails() {
    if (!root.selected || detailsProc.running) return
    detailsProc.command = ["python3", root.script, "--details", String(root.selected.pid), "--start", String(root.selected.start)]
    detailsProc.running = true
  }

  // Whole-group actions close apps: the first click turns the row itself into a
  // confirm/cancel pair, so the question shows where the user is looking.
  function armGroup(kind, g) {
    if (!g || root.busy !== "") return
    root.pending = kind + ":" + g.key
    pendingTimer.restart()
  }

  function runGroup(kind, g) {
    root.pending = ""
    if (!g || groupProc.running) return
    root.busy = g.key
    root.status = (kind === "stop" ? "Stopping " : "Restarting ") + g.name + "…"
    groupProc.command = ["python3", root.script, kind === "stop" ? "--stop-group" : "--restart-group", g.key]
    groupProc.running = true
  }

  function isTermed(row) { return row && root.termed[row.pid + ":" + row.start] === true }

  function stop(row) {
    var key = row.pid + ":" + row.start
    var force = root.termed[key] === true
    var next = Object.assign({}, root.termed)
    next[key] = true
    root.termed = next
    killProc.command = ["python3", root.script, "--kill", String(row.pid), "--start", String(row.start)]
      .concat(force ? ["--force"] : [])
    killProc.running = true
  }

  onOpenedChanged: {
    if (opened) { root.status = ""; refreshTop() }
    else { root.selected = null; root.details = null }
  }

  // No input for this long, inhibitors ignored: a video playing doesn't mean someone is there.
  IdleMonitor {
    id: idle
    timeout: root.setting("idleMinutes", 30) * 60
    respectInhibitors: false
    onIsIdleChanged: root.check()
  }

  Process {
    id: checkProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var d = JSON.parse(text)
          root.setFaults(d.issues, d.faults || [])
        } catch (e) { console.warn("idle-watch: " + e) }
      }
    }
  }

  Process {
    id: staleProc
    command: ["python3", root.script, "--stale"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var groups = JSON.parse(text).groups
          var keys = groups.map(g => g.key)
          root.stale = groups
          if (JSON.stringify(keys) !== JSON.stringify(root.staleKeys)) root.staleKeys = keys
        } catch (e) { console.warn("idle-watch: " + e) }
      }
    }
  }

  Process {
    id: faultsProc
    command: ["python3", root.script, "--faults"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try { root.setFaults(root.issues, JSON.parse(text).faults) } catch (e) { console.warn("idle-watch: " + e) }
      }
    }
  }

  Process {
    id: fixProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.status = String(text).trim()
    }
    onExited: { root.busy = ""; root.refreshTop() }
  }

  Process {
    id: groupProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.status = String(text).trim()
    }
    onExited: { root.busy = ""; root.staleGroup = ""; root.refreshTop(); root.check() }
  }

  Timer {
    id: pendingTimer
    interval: 15000
    onTriggered: root.pending = ""
  }

  Process {
    id: killProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.status = String(text).trim()
    }
    onExited: { root.refreshTop(); root.refreshDetails(); root.check() }
  }

  Process {
    id: detailsProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try { root.details = JSON.parse(text) } catch (e) { console.warn("idle-watch: " + e) }
      }
    }
  }

  Timer {
    interval: 60000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: { root.check(); if (!staleProc.running) staleProc.running = true }
  }

  Timer {
    interval: 3000
    running: root.opened
    repeat: true
    onTriggered: { root.refreshTop(); root.refreshDetails() }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.allOk ? "\uf00c" : "\uf071"
    active: root.urgent
    foreground: root.allOk ? root.okColor : root.fg
    slotSize: Style.bar.statusSlot
    fontSize: Style.font.caption
    tooltipText: root.allOk ? "All good"
      : root.issues.map(i => i.text).concat(root.faults.map(f => f.title))
          .concat(root.stale.map(g => g.name + ": " + root.plural(g.count, "process", "processes") + " on an old binary")).join("\n")
    onPressed: root.toggle()
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(root.selected ? 1040 : 580))
    contentHeight: panel.fittedContentHeight(Math.max(list.implicitHeight, detailsPane.visible ? detailsPane.implicitHeight : 0))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.selected ? root.select(root.selected) : root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: list
        anchors.left: parent.left
        anchors.top: parent.top
        width: Math.min(Style.space(580), parent.width)
        spacing: Style.space(4)

        Heading { visible: root.faultKeys.length > 0 || root.allOk; text: root.allOk ? "All good" : "Problems (" + root.faultKeys.length + ")" }

        Label {
          visible: root.allOk
          text: "\uf00c  No hung processes, failed services, crashes or stale binaries."
          color: root.okColor
        }

        Label {
          visible: root.status !== ""
          width: list.width
          text: root.status
          wrapMode: Text.Wrap
          font.bold: true
        }

        Repeater {
          model: root.faultKeys
          delegate: FaultRow { required property var modelData; key: modelData }
        }

        Heading {
          visible: root.stale.length > 0
          text: "Running from an old binary (" + root.plural(root.stale.reduce((n, g) => n + g.count, 0), "process", "processes") + ")"
        }
        Repeater {
          model: root.staleKeys
          delegate: StaleRow { required property var modelData; key: modelData }
        }

        Heading {
          visible: root.staleGroup !== ""
          text: root.groupFor(root.staleGroup) ? root.plural(root.groupFor(root.staleGroup).count, "process", "processes") + " of " + root.groupFor(root.staleGroup).name : ""
        }
        Repeater {
          model: root.staleGroup !== "" && root.groupFor(root.staleGroup) ? root.groupFor(root.staleGroup).procs : []
          delegate: ProcRow { required property var modelData; row: modelData }
        }

        Label {
          visible: !root.allOk
          text: root.staleGroup !== ""
            ? "● active now  ○ waiting  ⚠ suspect · click the group to collapse it"
            : "click a problem with a process, or a group, for details"
          opacity: 0.55
          topPadding: Style.space(6)
        }
      }

      Rectangle {
        visible: detailsPane.visible
        anchors.left: list.right
        anchors.leftMargin: Style.space(14)
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        width: 1
        color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.15)
      }

      Column {
        id: detailsPane
        visible: root.selected !== null
        anchors.left: list.right
        anchors.leftMargin: Style.space(29)
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(6)

        readonly property var d: root.details

        Item {
          width: parent.width
          implicitHeight: Math.max(title.implicitHeight, closeDetails.height)

          Label {
            id: title
            anchors.left: parent.left
            anchors.right: closeDetails.left
            anchors.verticalCenter: parent.verticalCenter
            text: detailsPane.d ? (detailsPane.d.gone ? "The process has exited" : detailsPane.d.name + "  ·  PID " + detailsPane.d.pid) : "Loading…"
            font.pixelSize: Style.font.title
            font.bold: true
            elide: Text.ElideRight
          }

          PanelActionButton {
            id: closeDetails
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            iconText: ""
            foreground: root.fg
            tooltipText: "Close details (Esc)"
            onClicked: root.select(root.selected)
          }
        }

        Label {
          visible: !!detailsPane.d && !detailsPane.d.gone
          width: parent.width
          text: detailsPane.d && detailsPane.d.cmd ? detailsPane.d.cmd : ""
          wrapMode: Text.WrapAnywhere
          maximumLineCount: 4
          elide: Text.ElideRight
          opacity: 0.7
        }

        Repeater {
          model: detailsPane.d && detailsPane.d.suspect ? detailsPane.d.suspect : []
          delegate: Label {
            required property var modelData
            width: detailsPane.width
            text: "⚠ Suspect: " + modelData
            color: Color.urgent
            wrapMode: Text.Wrap
          }
        }

        Repeater {
          model: detailsPane.d && detailsPane.d.fields ? detailsPane.d.fields : []
          delegate: Item {
            required property var modelData
            width: detailsPane.width
            implicitHeight: Math.max(fieldName.implicitHeight, fieldValue.implicitHeight)

            Label {
              id: fieldName
              width: Style.space(120)
              text: modelData[0]
              opacity: 0.55
            }
            Label {
              id: fieldValue
              anchors.left: fieldName.right
              anchors.right: parent.right
              text: modelData[1]
              wrapMode: Text.WrapAnywhere
            }
          }
        }

        Button {
          visible: !!detailsPane.d && !detailsPane.d.gone
          readonly property bool force: root.isTermed(detailsPane.d)
          text: force ? "Force stop (KILL)" : "Stop process (TERM)"
          iconText: force ? "" : ""
          foreground: force || (detailsPane.d && detailsPane.d.suspect && detailsPane.d.suspect.length) ? Color.urgent : root.fg
          bordered: true
          onClicked: root.stop(detailsPane.d)
        }
      }
    }
  }

  readonly property color fg: root.bar ? root.bar.foreground : Color.foreground

  component Label: Text {
    textFormat: Text.PlainText
    color: root.fg
    font.family: root.bar ? root.bar.fontFamily : Style.font.family
    font.pixelSize: Style.font.bodySmall
  }

  component Heading: Label {
    font.pixelSize: Style.font.caption
    font.bold: true
    font.letterSpacing: 1.2
    opacity: 0.6
    topPadding: Style.space(6)
  }

  component StaleRow: Item {
    id: staleRow
    property string key: ""
    readonly property var group: root.groupFor(key) || ({})
    readonly property string confirmKind: root.pending === "stop:" + key ? "stop" : (root.pending === "restart:" + key ? "restart" : "")
    readonly property bool isBusy: root.busy === key
    readonly property bool isOpen: root.staleGroup === group.key
    readonly property string pkgText: group.pkg
      ? group.ver + (group.upgraded ? " · upgraded " + group.upgraded : "")
      : "no package · " + group.key

    width: list.width
    implicitHeight: Math.max(stopAllButton.height, confirmButton.visible ? confirmButton.implicitHeight : 0, staleName.implicitHeight) + Style.space(4)

    Rectangle {
      anchors.fill: parent
      radius: Style.space(4)
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, staleRow.isOpen ? 0.14 : (staleMouse.containsMouse ? 0.07 : 0))
    }

    MouseArea {
      id: staleMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.toggleGroup(staleRow.group)
    }

    Label {
      id: staleName
      anchors.left: parent.left
      anchors.leftMargin: Style.space(4)
      anchors.right: staleRow.confirmKind !== "" ? confirmButton.left : staleLabel.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      text: (staleRow.isOpen ? "▾ " : "▸ ") + staleRow.group.name + "  ·  " + staleRow.pkgText
      color: Color.urgent
      elide: Text.ElideRight
    }

    Label {
      id: staleLabel
      anchors.right: claudeButton.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      visible: staleRow.confirmKind === ""
      text: staleRow.isBusy ? (root.status || "…") : root.plural(staleRow.group.count, "process", "processes") + " · " + staleRow.group.mem + " MB"
      opacity: 0.8
    }

    Button {
      id: confirmButton
      visible: staleRow.confirmKind !== ""
      anchors.right: cancelButton.left
      anchors.rightMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      text: staleRow.confirmKind === "stop"
        ? "Yes, stop " + root.plural(staleRow.group.count, "process", "processes")
        : "Yes, restart" + (staleRow.group.restart && staleRow.group.restart.kind === "desktop" ? " (windows will close)" : "")
      foreground: Color.urgent
      bordered: true
      onClicked: root.runGroup(staleRow.confirmKind, staleRow.group)
    }

    // Sits exactly where the stop button was, so a stray double-click cancels instead of confirming.
    PanelActionButton {
      id: cancelButton
      visible: staleRow.confirmKind !== ""
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      iconText: "\uf00d"
      foreground: root.fg
      tooltipText: "Cancel"
      onClicked: root.pending = ""
    }

    PanelActionButton {
      id: claudeButton
      anchors.right: restartButton.left
      anchors.verticalCenter: parent.verticalCenter
      visible: staleRow.confirmKind === "" && !staleRow.isBusy && !!staleRow.group.prompt
      iconText: "\uf0d0"
      foreground: root.fg
      tooltipText: "Repair with Claude (opens a new session)"
      onClicked: root.askClaude(staleRow.group.prompt)
    }

    PanelActionButton {
      id: restartButton
      anchors.right: stopAllButton.left
      anchors.verticalCenter: parent.verticalCenter
      visible: staleRow.confirmKind === "" && !staleRow.isBusy
      opacity: staleRow.group.restart ? 1 : 0.25
      enabled: !!staleRow.group.restart
      iconText: "\uf01e"
      foreground: root.fg
      tooltipText: staleRow.group.restart ? "Restart " + staleRow.group.name + " on the new version" : "I don't know how to restart it"
      onClicked: root.armGroup("restart", staleRow.group)
    }

    PanelActionButton {
      id: stopAllButton
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      visible: staleRow.confirmKind === "" && !staleRow.isBusy
      iconText: "\uf04d"
      foreground: root.fg
      tooltipText: "Stop " + root.plural(staleRow.group.count, "process", "processes")
      onClicked: root.armGroup("stop", staleRow.group)
    }
  }

  component FaultRow: Item {
    id: faultRow
    property string key: ""
    readonly property var f: root.faultFor(key) || ({})
    readonly property bool hot: f.urgent !== false
    readonly property bool clickable: f.pid !== undefined
    readonly property var confirming: {
      var acts = f.actions || []
      for (var i = 0; i < acts.length; i++) if (root.pending === "fix:" + acts[i].id + ":" + key) return acts[i]
      return null
    }

    width: list.width
    implicitHeight: Math.max(faultText.implicitHeight, faultActions.height, faultConfirm.visible ? faultConfirm.implicitHeight : 0) + Style.space(6)

    Rectangle {
      anchors.fill: parent
      radius: Style.space(4)
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, faultRow.clickable && faultMouse.containsMouse ? 0.07 : 0)
    }

    MouseArea {
      id: faultMouse
      anchors.fill: parent
      hoverEnabled: faultRow.clickable
      enabled: faultRow.clickable
      cursorShape: Qt.PointingHandCursor
      onClicked: root.select({ pid: faultRow.f.pid, start: faultRow.f.start })
    }

    Column {
      id: faultText
      anchors.left: parent.left
      anchors.leftMargin: Style.space(4)
      anchors.right: faultRow.confirming ? faultConfirm.left : faultActions.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter

      Label {
        width: parent.width
        text: (faultRow.hot ? "⚠ " : "○ ") + (faultRow.f.title || faultRow.f.text || "")
        color: faultRow.hot ? Color.urgent : root.fg
        wrapMode: Text.Wrap
      }
      Label {
        visible: text !== ""
        width: parent.width
        text: root.busy === faultRow.key ? (root.status || "…") : (faultRow.f.detail || "")
        elide: Text.ElideRight
        opacity: 0.6
      }
    }

    Button {
      id: faultConfirm
      visible: !!faultRow.confirming
      anchors.right: faultCancel.left
      anchors.rightMargin: Style.space(6)
      anchors.verticalCenter: parent.verticalCenter
      text: faultRow.confirming ? faultRow.confirming.confirm : ""
      foreground: Color.urgent
      bordered: true
      onClicked: root.fixFault(faultRow.f, faultRow.confirming.id)
    }

    // Same spot as the rightmost action, so a stray double-click cancels.
    PanelActionButton {
      id: faultCancel
      visible: !!faultRow.confirming
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      iconText: "\uf00d"
      foreground: root.fg
      tooltipText: "Cancel"
      onClicked: root.pending = ""
    }

    Row {
      id: faultActions
      visible: !faultRow.confirming && root.busy !== faultRow.key
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter

      PanelActionButton {
        visible: !!faultRow.f.prompt
        iconText: "\uf0d0"
        foreground: root.fg
        tooltipText: "Repair with Claude (opens a new session)"
        onClicked: root.askClaude(faultRow.f.prompt)
      }

      Repeater {
        model: faultRow.f.actions || []
        delegate: PanelActionButton {
          required property var modelData
          iconText: modelData.icon
          foreground: root.fg
          tooltipText: modelData.tip
          onClicked: {
            if (!modelData.confirm) return root.fixFault(faultRow.f, modelData.id)
            root.pending = "fix:" + modelData.id + ":" + faultRow.key
            pendingTimer.restart()
          }
        }
      }
    }
  }

  component ProcRow: Item {
    id: procRow
    property var row: ({})
    readonly property bool termed: root.isTermed(row)
    readonly property bool isSelected: !!root.selected && root.selected.pid === row.pid && root.selected.start === row.start
    readonly property bool suspect: !!row.suspect && row.suspect.length > 0

    width: list.width
    implicitHeight: Math.max(stopButton.height, name.implicitHeight) + Style.space(4)

    Rectangle {
      anchors.fill: parent
      radius: Style.space(4)
      color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, procRow.isSelected ? 0.14 : (rowMouse.containsMouse ? 0.07 : 0))
    }

    MouseArea {
      id: rowMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.select(procRow.row)
    }

    Label {
      id: dot
      anchors.left: parent.left
      anchors.leftMargin: Style.space(4)
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(16)
      text: procRow.suspect ? "⚠" : (procRow.row.active ? "●" : "○")
      color: procRow.suspect ? Color.urgent : root.fg
      opacity: procRow.suspect || procRow.row.active ? 1 : 0.5
    }

    Label {
      id: name
      anchors.left: dot.right
      anchors.right: stats.left
      anchors.rightMargin: Style.space(10)
      anchors.verticalCenter: parent.verticalCenter
      text: procRow.row.name + "  ·  " + procRow.row.cmd
      color: procRow.suspect ? Color.urgent : root.fg
      elide: Text.ElideRight
    }

    Label {
      id: stats
      anchors.right: stopButton.left
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      text: procRow.row.age + "  ·  " + procRow.row.cpu + "%  ·  " + procRow.row.mem + " MB"
      opacity: 0.8
    }

    PanelActionButton {
      id: stopButton
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      iconText: procRow.termed ? "" : ""
      foreground: procRow.termed ? Color.urgent : root.fg
      tooltipText: procRow.termed ? "Force stop (KILL)" : "Stop (TERM) · PID " + procRow.row.pid
      onClicked: root.stop(procRow.row)
    }
  }
}
