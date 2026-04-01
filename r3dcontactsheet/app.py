"""Minimal macOS-first desktop app for batch REDline still renders."""

from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path
from tkinter import BooleanVar, END, LEFT, RIGHT, StringVar, Text, Tk, filedialog, messagebox, ttk

from .batch import BatchOptions, GroupMode, build_job_plan, discover_r3d_clips
from .frame_index import FrameTargetRequest
from .redline import (
    RedlinePaths,
    RenderSettings,
    probe_redline,
    render_frame,
    shell_join,
    write_batch_file,
)
from .settings import AppSettings, SettingsStore


APP_TITLE = "R3D Contact Sheet"
MIN_OUTPUT_BYTES = 2048


class App:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1220x760")
        self.root.minsize(1100, 700)

        self.store = SettingsStore()
        self.settings = self.store.load()
        self.plan = []
        self.worker: threading.Thread | None = None
        self.event_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.run_started_at = 0.0

        self.redline_path = StringVar(value=self.settings.redline_path)
        self.input_path = StringVar(value=self.settings.last_input_path)
        self.output_path = StringVar(value=self.settings.last_output_path)
        self.frame_index = StringVar(value=str(self.settings.frame_index))
        self.target_timecode = StringVar(value=self.settings.target_timecode)
        self.fps = StringVar(value=self.settings.fps)
        self.drop_frame = BooleanVar(value=self.settings.drop_frame)
        self.color_sci_version = StringVar(value=str(self.settings.color_sci_version))
        self.output_tone_map = StringVar(value=str(self.settings.output_tone_map))
        self.roll_off = StringVar(value=str(self.settings.roll_off))
        self.output_gamma_curve = StringVar(value=str(self.settings.output_gamma_curve))
        self.render_res = StringVar(value=str(self.settings.render_res))
        self.resize_x = StringVar(value=self.settings.resize_x)
        self.resize_y = StringVar(value=self.settings.resize_y)
        self.group_mode = StringVar(value=self.settings.group_mode)
        self.alphabetize = BooleanVar(value=self.settings.alphabetize)
        self.metadata_mode = BooleanVar(value=self.settings.metadata_mode)
        self.status_message = StringVar(value="Choose clips or a reel folder to begin.")
        self.summary_message = StringVar(value="No jobs planned yet.")

        self._build_ui()
        self._refresh_redline_probe()
        self._schedule_poll()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)

        header = ttk.Frame(self.root, padding=14)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        ttk.Label(header, text=APP_TITLE, font=("SF Pro Display", 20, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(header, textvariable=self.status_message, foreground="#3D4C63").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )

        paths = ttk.LabelFrame(self.root, text="Paths", padding=12)
        paths.grid(row=1, column=0, sticky="ew", padx=14)
        paths.columnconfigure(1, weight=1)

        self._path_row(paths, 0, "REDline", self.redline_path, self._choose_redline)
        self._source_row(paths, 1)
        self._path_row(paths, 2, "Output", self.output_path, self._choose_output)

        controls = ttk.Frame(self.root, padding=(14, 10))
        controls.grid(row=2, column=0, sticky="ew")
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        left = ttk.LabelFrame(controls, text="Frame Target", padding=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(1, weight=1)

        ttk.Label(left, text="Frame index").grid(row=0, column=0, sticky="w")
        ttk.Entry(left, textvariable=self.frame_index, width=14).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(left, text="Target timecode").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(left, textvariable=self.target_timecode).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(8, 0))
        ttk.Label(left, text="FPS").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(left, textvariable=self.fps, width=14).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Checkbutton(left, text="Drop-frame timecode", variable=self.drop_frame).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        ttk.Checkbutton(left, text="Metadata mode (default recommended)", variable=self.metadata_mode).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        right = ttk.LabelFrame(controls, text="Render Settings", padding=12)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        right.columnconfigure(1, weight=1)
        self._labeled_entry(right, 0, "Color science", self.color_sci_version)
        self._labeled_entry(right, 1, "Output tone map", self.output_tone_map)
        self._labeled_entry(right, 2, "Roll off", self.roll_off)
        self._labeled_entry(right, 3, "Output gamma", self.output_gamma_curve)
        self._labeled_entry(right, 4, "Render res", self.render_res)
        self._labeled_entry(right, 5, "Resize X", self.resize_x)
        self._labeled_entry(right, 6, "Resize Y", self.resize_y)

        batch_frame = ttk.LabelFrame(self.root, text="Batch Planning", padding=12)
        batch_frame.grid(row=3, column=0, sticky="nsew", padx=14, pady=(0, 10))
        batch_frame.columnconfigure(0, weight=3)
        batch_frame.columnconfigure(1, weight=2)
        batch_frame.rowconfigure(1, weight=1)

        options = ttk.Frame(batch_frame)
        options.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Label(options, text="Grouping").pack(side=LEFT)
        ttk.Combobox(
            options,
            textvariable=self.group_mode,
            values=("flat", "parent_folder", "reel_prefix"),
            state="readonly",
            width=16,
        ).pack(side=LEFT, padx=(8, 18))
        ttk.Checkbutton(options, text="Alphabetize clips", variable=self.alphabetize).pack(side=LEFT)
        ttk.Button(options, text="Preview Jobs", command=self.preview_jobs).pack(side=RIGHT)

        columns = ("clip", "group", "frame", "output")
        self.tree = ttk.Treeview(batch_frame, columns=columns, show="headings", height=14)
        for name, label, width in (
            ("clip", "Clip", 220),
            ("group", "Group", 150),
            ("frame", "Frame", 70),
            ("output", "Output", 420),
        ):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, anchor="w")
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(0, 10))

        log_panel = ttk.Frame(batch_frame)
        log_panel.grid(row=1, column=1, sticky="nsew")
        log_panel.rowconfigure(1, weight=1)
        log_panel.columnconfigure(0, weight=1)

        ttk.Label(log_panel, textvariable=self.summary_message).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.log_text = Text(log_panel, wrap="word", height=18)
        self.log_text.grid(row=1, column=0, sticky="nsew")

        footer = ttk.Frame(self.root, padding=(14, 0, 14, 14))
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(footer, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        ttk.Button(footer, text="Save Settings", command=self._save_settings).grid(row=0, column=1, padx=(0, 8))
        self.run_button = ttk.Button(footer, text="Render Batch", command=self.run_jobs)
        self.run_button.grid(row=0, column=2)

    def _path_row(self, parent: ttk.Frame, row: int, label: str, variable: StringVar, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(parent, text="Choose…", command=command).grid(row=row, column=2, sticky="e")

    def _source_row(self, parent: ttk.Frame, row: int) -> None:
        ttk.Label(parent, text="Source").grid(row=row, column=0, sticky="w")
        ttk.Entry(parent, textvariable=self.input_path).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        button_wrap = ttk.Frame(parent)
        button_wrap.grid(row=row, column=2, sticky="e")
        ttk.Button(button_wrap, text="Clip…", command=self._choose_input_file).pack(side=LEFT)
        ttk.Button(button_wrap, text="Folder…", command=self._choose_input_folder).pack(side=LEFT, padx=(6, 0))

    def _labeled_entry(self, parent: ttk.Frame, row: int, label: str, variable: StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=4)

    def _choose_redline(self) -> None:
        path = filedialog.askopenfilename(title="Choose REDline executable")
        if path:
            self.redline_path.set(path)
            self._save_settings()
            self._refresh_redline_probe()

    def _choose_input_file(self) -> None:
        selection = filedialog.askopenfilename(
            title="Choose an R3D clip",
            filetypes=[("RED clip", "*.R3D"), ("All files", "*.*")],
        )
        if selection:
            self.input_path.set(selection)
            self._save_settings()

    def _choose_input_folder(self) -> None:
        selection = filedialog.askdirectory(title="Choose a reel or source folder")
        if selection:
            self.input_path.set(selection)
            self._save_settings()

    def _choose_output(self) -> None:
        path = filedialog.askdirectory(title="Choose output folder")
        if path:
            self.output_path.set(path)
            self._save_settings()

    def _refresh_redline_probe(self) -> None:
        explicit = Path(self.redline_path.get()).expanduser() if self.redline_path.get().strip() else None
        probe = probe_redline(paths=None if explicit is None else RedlinePaths(explicit_path=explicit))
        if probe.available and probe.executable:
            self.redline_path.set(str(probe.executable))
        self.status_message.set(probe.message)

    def preview_jobs(self) -> None:
        try:
            plan = self._build_plan()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self.plan = plan
        batch_path = Path(self.output_path.get().strip()).expanduser().resolve() / "r3dcontactsheet_last_batch.sh"
        write_batch_file(
            [item.render_job for item in plan],
            batch_path,
            redline_exe=self.redline_path.get().strip(),
        )
        self._render_plan()
        self.summary_message.set(f"{len(plan)} jobs ready. Batch script saved.")
        self._append_log(f"Prepared {len(plan)} jobs.")
        self._append_log(f"Saved replay script: {batch_path}")

    def run_jobs(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            if not self.plan:
                self.plan = self._build_plan()
                self._render_plan()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self._save_settings()
        self.run_button.state(["disabled"])
        self.progress.configure(value=0, maximum=max(len(self.plan), 1))
        self.run_started_at = time.time()
        self._append_log(f"Starting render for {len(self.plan)} job(s).")
        self.worker = threading.Thread(target=self._run_jobs_worker, daemon=True)
        self.worker.start()

    def _run_jobs_worker(self) -> None:
        successes = 0
        failures = 0
        for index, item in enumerate(self.plan, start=1):
            started = time.time()
            try:
                result = render_frame(
                    item.render_job,
                    redline_exe=self.redline_path.get(),
                    min_output_bytes=MIN_OUTPUT_BYTES,
                )
                command_text = shell_join(result.command)
                duration = time.time() - started
                self.event_queue.put(
                    (
                        "job-success",
                        {
                            "index": index,
                            "command": command_text,
                            "output": str(result.job.output_file),
                            "size": result.output_size,
                            "duration": duration,
                        },
                    )
                )
                successes += 1
            except Exception as exc:
                failures += 1
                self.event_queue.put(
                    (
                        "job-failure",
                        {
                            "index": index,
                            "error": str(exc),
                            "traceback": traceback.format_exc(),
                        },
                    )
                )
            finally:
                self.event_queue.put(("progress", {"completed": index, "total": len(self.plan)}))

        elapsed = time.time() - self.run_started_at
        self.event_queue.put(
            (
                "done",
                {
                    "successes": successes,
                    "failures": failures,
                    "elapsed": elapsed,
                },
            )
        )

    def _build_plan(self):
        input_value = self.input_path.get().strip()
        output_value = self.output_path.get().strip()
        if not input_value:
            raise ValueError("Choose an R3D clip or source folder.")
        if not output_value:
            raise ValueError("Choose an output folder.")
        if not self.redline_path.get().strip():
            raise ValueError("Choose a REDline executable.")

        frame_request = self._build_frame_request()
        settings = self._build_render_settings()
        clips = discover_r3d_clips(
            Path(input_value),
            group_mode=self.group_mode.get(),  # type: ignore[arg-type]
            alphabetize=self.alphabetize.get(),
        )
        options = BatchOptions(
            output_dir=Path(output_value),
            frame_request=frame_request,
            settings=settings,
            group_mode=self.group_mode.get(),  # type: ignore[arg-type]
            alphabetize=self.alphabetize.get(),
        )
        return build_job_plan(clips, options)

    def _build_frame_request(self) -> FrameTargetRequest:
        frame_text = self.frame_index.get().strip()
        tc_text = self.target_timecode.get().strip()
        fps_text = self.fps.get().strip()

        frame_value = int(frame_text) if frame_text else None
        fps_value = float(fps_text) if fps_text else None
        if frame_value is None and not tc_text:
            raise ValueError("Enter either a frame index or a target timecode.")
        return FrameTargetRequest(
            frame_index=frame_value,
            target_timecode=tc_text or None,
            fps=fps_value,
            drop_frame=self.drop_frame.get(),
            verify_matching_frame=True,
        )

    def _build_render_settings(self) -> RenderSettings:
        resize_x = int(self.resize_x.get()) if self.resize_x.get().strip() else None
        resize_y = int(self.resize_y.get()) if self.resize_y.get().strip() else None
        return RenderSettings(
            render_res=int(self.render_res.get()),
            resize_x=resize_x,
            resize_y=resize_y,
            use_meta=self.metadata_mode.get(),
            color_sci_version=int(self.color_sci_version.get()),
            output_tone_map=int(self.output_tone_map.get()),
            roll_off=int(self.roll_off.get()),
            output_gamma_curve=int(self.output_gamma_curve.get()),
        )

    def _render_plan(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for item in self.plan:
            self.tree.insert(
                "",
                END,
                values=(
                    item.clip.clip_name,
                    item.clip.group_name,
                    item.frame_resolution.frame_index,
                    str(item.output_file),
                ),
            )

    def _save_settings(self) -> None:
        settings = AppSettings(
            redline_path=self.redline_path.get().strip(),
            last_input_path=self.input_path.get().strip(),
            last_output_path=self.output_path.get().strip(),
            frame_index=int(self.frame_index.get().strip() or "6"),
            target_timecode=self.target_timecode.get().strip(),
            fps=self.fps.get().strip() or "23.976",
            drop_frame=self.drop_frame.get(),
            color_sci_version=int(self.color_sci_version.get().strip() or "3"),
            output_tone_map=int(self.output_tone_map.get().strip() or "1"),
            roll_off=int(self.roll_off.get().strip() or "2"),
            output_gamma_curve=int(self.output_gamma_curve.get().strip() or "32"),
            render_res=int(self.render_res.get().strip() or "4"),
            resize_x=self.resize_x.get().strip(),
            resize_y=self.resize_y.get().strip(),
            group_mode=self.group_mode.get().strip() or "flat",
            alphabetize=self.alphabetize.get(),
            metadata_mode=self.metadata_mode.get(),
        )
        self.store.save(settings)
        self.summary_message.set("Settings saved.")

    def _append_log(self, text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert(END, f"[{timestamp}] {text}\n")
        self.log_text.see(END)

    def _schedule_poll(self) -> None:
        self.root.after(150, self._poll_events)

    def _poll_events(self) -> None:
        while True:
            try:
                event, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if event == "job-success":
                self._on_job_success(payload)
            elif event == "job-failure":
                self._on_job_failure(payload)
            elif event == "progress":
                self._on_progress(payload)
            elif event == "done":
                self._on_done(payload)
        self._schedule_poll()

    def _on_job_success(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self._append_log(
            f"Job {data['index']} succeeded in {data['duration']:.1f}s, {data['size']} bytes."
        )
        self._append_log(f"Command: {data['command']}")

    def _on_job_failure(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self._append_log(f"Job {data['index']} failed.")
        self._append_log(str(data["error"]))

    def _on_progress(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        completed = data["completed"]
        total = data["total"]
        self.progress.configure(value=completed, maximum=total)
        elapsed = time.time() - self.run_started_at if self.run_started_at else 0.0
        eta = (elapsed / completed) * (total - completed) if completed else 0.0
        self.summary_message.set(f"{completed}/{total} complete. ETA {eta:.1f}s.")

    def _on_done(self, payload: object) -> None:
        data = payload  # type: ignore[assignment]
        self.run_button.state(["!disabled"])
        self.summary_message.set(
            f"Finished in {data['elapsed']:.1f}s. {data['successes']} succeeded, {data['failures']} failed."
        )
        if data["failures"]:
            messagebox.showwarning(
                APP_TITLE,
                f"Batch finished with {data['failures']} failure(s). Review the log for details.",
            )
        else:
            messagebox.showinfo(APP_TITLE, "Batch finished successfully.")
def main() -> None:
    root = Tk()
    try:
        ttk.Style().theme_use("aqua")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
