import json
import os
import sys
import tkinter as tk
from tkinter import messagebox, ttk

import requests

BASE_URL = "https://platform-api.productsup.io/platform/v2"
TOKEN = os.getenv("PRODUCTSUP_TOKEN", "")
HEADERS = {"X-Auth-Token": TOKEN}

if not TOKEN:
    print("[ERROR] PRODUCTSUP_TOKEN not set.")
    sys.exit(1)


class ProjectSelector:
    def __init__(self, root):
        self.root = root
        self.root.title("Select Projects to Monitor")
        self.root.geometry("1024x1024")

        self.projects = []  # list of project dicts
        self.project_vars = {}  # project_id -> BooleanVar
        self.site_vars = {}  # (project_id, site_id) -> BooleanVar
        self.project_sites = {}  # project_id -> list of sites

        self.setup_ui()
        self.load_projects()

    def setup_ui(self):
        # Top frame
        top = tk.Frame(self.root)
        top.pack(fill=tk.X, padx=10, pady=10)
        self.search_var = tk.StringVar()
        self.search_var.trace("w", lambda *args: self.filter_projects())
        tk.Label(top, text="Search:").pack(side=tk.LEFT)
        tk.Entry(top, textvariable=self.search_var, width=30).pack(side=tk.LEFT, padx=5)

        # Canvas for scrolling
        self.canvas = tk.Canvas(self.root)
        scrollbar = ttk.Scrollbar(
            self.root, orient="vertical", command=self.canvas.yview
        )
        self.scroll_frame = tk.Frame(self.canvas)

        self.scroll_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Bottom buttons
        bottom = tk.Frame(self.root)
        bottom.pack(fill=tk.X, padx=10, pady=10)
        tk.Button(
            bottom,
            text="Export Config",
            command=self.export_config,
            bg="#7c5cfc",
            fg="white",
        ).pack(fill=tk.X)
        tk.Label(
            bottom, text="Tip: Check project to monitor all its active sites."
        ).pack(pady=5)

    def load_projects(self):
        """Fetch all projects from API."""
        try:
            resp = requests.get(f"{BASE_URL}/projects", headers=HEADERS, timeout=30)
            data = resp.json()
            if not data.get("success"):
                messagebox.showerror("Error", "Failed to load projects")
                return
            self.projects = data.get("Projects", [])
            self.render_projects()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load projects: {e}")

    def load_sites(self, project_id):
        """Load sites for a project lazily when expanded."""
        if project_id in self.project_sites:
            return
        try:
            resp = requests.get(
                f"{BASE_URL}/projects/{project_id}/sites", headers=HEADERS, timeout=30
            )
            data = resp.json()
            if data.get("success"):
                sites = data.get("Sites", [])
                self.project_sites[project_id] = sites
                # Create site vars
                for s in sites:
                    self.site_vars[(project_id, s.get("id"))] = tk.BooleanVar()
        except Exception as e:
            print(f"Failed to load sites for project {project_id}: {e}")

    def render_projects(self, filter_text=""):
        """Render projects in scrollable frame."""
        # Clear existing widgets
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()

        for p in self.projects:
            name = p.get("name", "")
            if filter_text and filter_text.lower() not in name.lower():
                continue
            pid = p.get("id")
            var = tk.BooleanVar()
            self.project_vars[pid] = var

            # Project row
            row = tk.Frame(self.scroll_frame)
            row.pack(fill=tk.X, pady=2)
            cb = tk.Checkbutton(
                row,
                text=f"{name} (ID: {pid})",
                variable=var,
                command=lambda pid=pid: self.toggle_project(pid),
            )
            cb.pack(side=tk.LEFT)

            # Expand button (placeholder)
            expand_btn = tk.Button(
                row,
                text="+",
                width=2,
                command=lambda pid=pid, row=row: self.toggle_expand(pid, row),
            )
            expand_btn.pack(side=tk.RIGHT)

            # Store reference to row
            row.project_id = pid
            row.expanded = False
            row.site_frame = None

    def toggle_project(self, pid):
        """Handle project checkbox toggle."""
        var = self.project_vars.get(pid)
        if var and var.get():
            # If project selected, load sites (in background)
            self.load_sites(pid)

    def toggle_expand(self, pid, row):
        """Expand/collapse project to show sites."""
        if row.expanded:
            if row.site_frame:
                row.site_frame.destroy()
                row.site_frame = None
            row.expanded = False
            return

        # Load sites if not already loaded
        self.load_sites(pid)
        sites = self.project_sites.get(pid, [])
        row.expanded = True

        # Create site frame
        site_frame = tk.Frame(row)
        site_frame.pack(fill=tk.X, padx=20)
        for s in sites:
            site_id = s.get("id")
            site_name = s.get("title") or s.get("name", "")
            status = s.get("status", "")
            svar = self.site_vars.get((pid, site_id), tk.BooleanVar())
            self.site_vars[(pid, site_id)] = svar
            tk.Checkbutton(
                site_frame, text=f"{site_name} ({site_id}) - {status}", variable=svar
            ).pack(anchor=tk.W)
        row.site_frame = site_frame

    def filter_projects(self):
        """Filter displayed projects by search text."""
        self.render_projects(self.search_var.get())

    def export_config(self):
        """Export monitor_projects.json."""
        config = {"projects": []}
        for pid, var in self.project_vars.items():
            if var.get():
                project = next((p for p in self.projects if p.get("id") == pid), None)
                if not project:
                    continue
                entry = {
                    "project_id": pid,
                    "project_name": project.get("name", ""),
                    "sites": [],
                }
                # Check selected sites
                for (proj_id, site_id), svar in self.site_vars.items():
                    if proj_id == pid and svar.get():
                        # Find site name
                        site_name = ""
                        for s in self.project_sites.get(pid, []):
                            if s.get("id") == site_id:
                                site_name = s.get("title") or s.get("name", "")
                                break
                        entry["sites"].append(
                            {"site_id": site_id, "site_name": site_name}
                        )
                config["projects"].append(entry)

        with open("monitor_projects.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        messagebox.showinfo("Success", "monitor_projects.json exported successfully")


def main():
    root = tk.Tk()
    ProjectSelector(root)
    root.mainloop()


if __name__ == "__main__":
    main()
