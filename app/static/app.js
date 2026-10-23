// No inline scripts are allowed by the Content-Security-Policy, so all behaviour lives here.
(function () {
  "use strict";

  var BR_NAME = { site_a: "Site A", site_b: "Site B", "": "no site" };

  // Open a dialog and fill it from the clicked element's data-* attributes.
  document.addEventListener("click", function (e) {
    var opener = e.target.closest("[data-open]");
    if (opener) {
      var dlg = document.getElementById(opener.dataset.open);
      if (!dlg) return;
      if (dlg.id === "dlg-staff") {
        var editing = !!opener.dataset.id;
        document.getElementById("staff-id").value = opener.dataset.id || "";
        document.getElementById("staff-name").value = opener.dataset.name || "";
        document.getElementById("staff-phone").value = opener.dataset.phone || "";
        document.getElementById("staff-title").textContent = editing ? "Edit" : "Add a person";
        var brRow = document.getElementById("staff-branch-row");
        brRow.hidden = editing || brRow.hasAttribute("data-fixed");   // an existing person changes branch with the switch
      }
      dlg.showModal();
      return;
    }
    var closer = e.target.closest("[data-close]");
    if (closer) {
      closer.closest("dialog").close();
      return;
    }
    // Click on the dark backdrop closes the dialog.
    if (e.target.tagName === "DIALOG") e.target.close();
  });

  // Public page: reload every 60 s (only while the tab is visible) so a shift change made in the
  // admin panel shows up without anyone pressing F5, and the page rolls over to the new day.
  var auto = document.querySelector("[data-refresh]");
  if (auto) {
    window.setInterval(function () {
      if (!document.hidden) window.location.reload();
    }, Number(auto.dataset.refresh) * 1000);
  }

  // Staff page: the Site A / Site B switch asks for confirmation before anything is changed.
  var brDlg = document.getElementById("dlg-branch");
  if (brDlg) {
    document.addEventListener("click", function (e) {
      var sw = e.target.closest(".swx");
      if (!sw) return;
      var old = sw.dataset.branch || "";
      var target;
      if (e.target.closest(".lb")) target = e.target.closest(".lb").dataset.b;
      else if (e.target.closest(".trk")) target = old === "site_a" ? "site_b" : "site_a";
      else return;
      if (target === old) return;
      var form = document.getElementById("frm-branch");
      form.action = form.dataset.url.replace("/0/", "/" + sw.dataset.id + "/");
      document.getElementById("br-value").value = target;
      document.getElementById("br-who").textContent = sw.dataset.name + ": " + BR_NAME[old] + " → " + BR_NAME[target];
      var future = Number(sw.dataset.future || 0);
      var note = document.getElementById("br-future");
      if (old && future) {
        note.textContent = "This person's upcoming duty days (" + future + " day(s), today included) at the old site become \"not assigned\". Past days stay as history.";
        note.hidden = false;
      } else {
        note.hidden = true;
      }
      brDlg.showModal();
    });
  }

  // Audit log: select all rows (owner).
  var selAll = document.getElementById("sel-all");
  if (selAll) {
    selAll.addEventListener("change", function () {
      Array.prototype.forEach.call(document.querySelectorAll(".sel-row"), function (c) { c.checked = selAll.checked; });
    });
  }

  // Schedule: every day has two lines (site_a, site_b). A person always goes to THEIR branch's line.
  var palette = document.getElementById("palette");
  if (palette) {
    var hint = document.getElementById("hint");
    var swapBtn = document.getElementById("swap-toggle");
    var multiBtn = document.getElementById("multi-toggle");
    var bar = document.getElementById("bulkbar");
    // staff: chosen palette chip (null = none, "" = the clear chip), branch: its branch
    var state = { staff: null, branch: "", name: "", swap: false, first: null, multi: false, sel: [] };

    var field = function (form, name) { return document.getElementById(form).elements[name]; };
    var show = function (text) {
      hint.textContent = text || "";
      hint.hidden = !text;
    };
    var lineText = function (ln) { return ln.closest(".day").dataset.label + " · " + BR_NAME[ln.dataset.branch]; };
    var refresh = function () {
      Array.prototype.forEach.call(palette.querySelectorAll(".chip"), function (c) {
        c.classList.toggle("on", state.staff !== null && c.dataset.staffId === state.staff);
      });
      Array.prototype.forEach.call(document.querySelectorAll(".ln"), function (l) {
        l.classList.toggle("pick", state.first === l);
      });
      Array.prototype.forEach.call(document.querySelectorAll(".day"), function (d) {
        d.classList.toggle("sel", state.sel.indexOf(d) !== -1);
      });
      multiBtn.setAttribute("aria-pressed", state.multi ? "true" : "false");
      multiBtn.classList.toggle("primary", state.multi);
      bar.hidden = !state.multi;
      document.getElementById("bulk-count").textContent = "Selected: " + state.sel.length + " day(s)";
      ["bulk-assign", "bulk-clear-site_a", "bulk-clear-site_b"].forEach(function (id) {
        document.getElementById(id).disabled = !state.sel.length;
      });
      swapBtn.setAttribute("aria-pressed", state.swap ? "true" : "false");
      swapBtn.classList.toggle("primary", state.swap);
      if (state.multi) show("Click days to select them (click again to deselect). Then choose a person and press \"Assign\", or unassign one site's person.");
      else if (state.swap) show(state.first ? "Choose the second day at the same site (first: " + lineText(state.first) + ")" : "Choose the first line to swap");
      else if (state.staff === "") show("Click the person in the table to remove. Press Esc to cancel.");
      else if (state.staff !== null) show("Choose a day for " + state.name + " (" + BR_NAME[state.branch] + "). Press Esc to cancel.");
      else show("");
    };
    var reset = function () {
      state = { staff: null, branch: "", name: "", swap: false, first: null, multi: false, sel: [] };
      refresh();
    };
    var assign = function (day, staffId, branch, remove) {
      field("frm-quick-assign", "day").value = day;
      field("frm-quick-assign", "staff_id").value = staffId;
      field("frm-quick-assign", "branch").value = branch;
      field("frm-quick-assign", "remove").value = remove || "";
      document.getElementById("frm-quick-assign").submit();
    };
    var swap = function (a, b, branch) {
      if (a === b) return;
      field("frm-swap", "a").value = a;
      field("frm-swap", "b").value = b;
      field("frm-swap", "branch").value = branch;
      document.getElementById("frm-swap").submit();
    };
    var bulk = function (staffId, branch) {
      field("frm-bulk", "days").value = state.sel.map(function (d) { return d.dataset.day; }).join(",");
      field("frm-bulk", "staff_id").value = staffId;
      field("frm-bulk", "branch").value = branch;
      document.getElementById("frm-bulk").submit();
    };

    palette.addEventListener("click", function (e) {
      var chip = e.target.closest(".chip[data-staff-id]");
      if (!chip) return;
      state.swap = false; state.first = null;
      if (state.multi) {
        if (chip.dataset.staffId) document.getElementById("bulk-staff").value = chip.dataset.staffId;
        return;
      }
      if (state.staff === chip.dataset.staffId) { reset(); return; }
      state.staff = chip.dataset.staffId;
      state.branch = chip.dataset.branch;
      state.name = chip.dataset.name;
      refresh();
    });
    swapBtn.addEventListener("click", function () {
      var on = !state.swap;
      reset();
      state.swap = on;
      refresh();
    });
    multiBtn.addEventListener("click", function () {
      var on = !state.multi;
      reset();
      state.multi = on;
      refresh();
    });
    document.getElementById("bulk-assign").addEventListener("click", function () {
      var s = document.getElementById("bulk-staff").value;
      if (!s) { show("First choose a person from the list or the palette."); return; }
      bulk(s, "");
    });
    ["site_a", "site_b"].forEach(function (b) {
      document.getElementById("bulk-clear-" + b).addEventListener("click", function () {
        if (window.confirm("The person on duty at " + BR_NAME[b] + " will be removed from " + state.sel.length + " day(s). Continue?")) bulk("", b);
      });
    });
    document.getElementById("bulk-cancel").addEventListener("click", reset);
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") reset(); });

    // Capture phase, so a click is handled by the active mode before anything else sees it.
    document.addEventListener("click", function (e) {
      var ln = e.target.closest(".ln");
      var day = e.target.closest(".day");
      if (!day) return;
      if (ln && ln.classList.contains("ro")) {           // the other branch: view only
        e.preventDefault(); e.stopImmediatePropagation();
        show("You are not allowed to manage that site.");
        return;
      }
      if (state.multi) {
        e.preventDefault();
        e.stopImmediatePropagation();
        var i = state.sel.indexOf(day);
        if (i === -1) state.sel.push(day); else state.sel.splice(i, 1);
        refresh();
        return;
      }
      if (!ln) return;
      var dayIso = day.dataset.day;
      if (state.swap) {
        if (state.first === ln) { state.first = null; refresh(); return; }
        if (!state.first) {
          if (!ln.dataset.staff || !ln.dataset.branch) { show("This line is empty. Choose a line that has a person assigned."); return; }
          state.first = ln; refresh(); return;
        }
        if (ln.dataset.branch !== state.first.dataset.branch) { show("A swap is only possible within one site."); return; }
        swap(state.first.closest(".day").dataset.day, dayIso, ln.dataset.branch);
        return;
      }
      if (state.staff === "") {                 // the remove chip: take this person off that day
        if (!ln.dataset.staff) { show("This line is empty."); return; }
        assign(dayIso, ln.dataset.staff, ln.dataset.branch, "1");
        return;
      }
      if (state.staff !== null) {
        assign(dayIso, state.staff, state.branch);
        return;
      }
      // Nothing chosen: open the dialog for this line, with only that branch's people.
      var branch = ln.dataset.branch;
      document.getElementById("assign-day").value = dayIso;
      document.getElementById("assign-branch").value = branch;
      document.getElementById("assign-label").textContent = lineText(ln);
      var chosen = {};
      Array.prototype.forEach.call(day.querySelectorAll('.ln[data-branch="' + branch + '"]'), function (l) {
        if (l.dataset.staff) chosen[l.dataset.staff] = true;
      });
      Array.prototype.forEach.call(document.querySelectorAll("#assign-list .chk"), function (lab) {
        var box = lab.querySelector("input");
        lab.hidden = lab.dataset.branch !== branch;
        box.disabled = lab.hidden;
        box.checked = !lab.hidden && !!chosen[box.value];
      });
      document.getElementById("assign-legacy").hidden = branch !== "";
      document.getElementById("dlg-assign").showModal();
    }, true);

    // Drag and drop: a person onto a day puts them on their branch's line; a filled line onto another line of the same branch swaps.
    Array.prototype.forEach.call(document.querySelectorAll(".ln"), function (l) {
      if (l.dataset.staff && l.dataset.branch && !l.classList.contains("ro")) l.setAttribute("draggable", "true");
    });
    document.addEventListener("dragstart", function (e) {
      var chip = e.target.closest && e.target.closest(".chip[draggable]");
      var ln = e.target.closest && e.target.closest(".ln[draggable]");
      if (chip) e.dataTransfer.setData("text/plain", "staff:" + chip.dataset.staffId + ":" + chip.dataset.branch);
      else if (ln) e.dataTransfer.setData("text/plain", "line:" + ln.closest(".day").dataset.day + ":" + ln.dataset.branch);
      else return;
      e.dataTransfer.effectAllowed = "move";
    });
    document.addEventListener("dragover", function (e) {
      var day = e.target.closest && e.target.closest(".day");
      if (!day) return;
      e.preventDefault();
      day.classList.add("drop");
    });
    document.addEventListener("dragleave", function (e) {
      var day = e.target.closest && e.target.closest(".day");
      if (day) day.classList.remove("drop");
    });
    document.addEventListener("drop", function (e) {
      var day = e.target.closest && e.target.closest(".day");
      if (!day) return;
      e.preventDefault();
      day.classList.remove("drop");
      var data = (e.dataTransfer.getData("text/plain") || "").split(":");
      if (data[0] === "staff") assign(day.dataset.day, data[1], data[2]);
      else if (data[0] === "line") swap(data[1], day.dataset.day, data[2]);
    });
  }

  // Selects marked data-autosubmit save as soon as they change.
  document.addEventListener("change", function (e) {
    if (e.target.matches && e.target.matches("select[data-autosubmit]")) e.target.form.submit();
  });

  // Ask before risky form submissions (data-confirm="message" on the form or on the submit button).
  document.addEventListener("submit", function (e) {
    var msg = (e.submitter && e.submitter.dataset.confirm) || (e.target.dataset && e.target.dataset.confirm);
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
})();
