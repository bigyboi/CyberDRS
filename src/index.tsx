import { callable, definePlugin, toaster, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { useEffect, useState, type CSSProperties } from "react";
import { FaCrosshairs } from "react-icons/fa";

interface Settings {
  enabled: boolean;
  scale: number;
  opacity: number;
  margin: number;
  position: string;
  show_output: boolean;
  candidate: string;
  display: string;
}

interface Candidate {
  w: number;
  h: number;
  n: number;
}

interface ProbeInfo {
  compiler: string | null;
  headers: boolean;
  built: boolean;
  installed: boolean;
  so_path: string;
  json_path: string;
  live: boolean;
}

interface Status {
  settings: Settings;
  running: boolean;
  game_pid: number | null;
  overlay: boolean;
  overlay_pid: number | null;
  mode: "LIVE" | "EST" | "WAIT" | "OFF";
  width: number | null;
  height: number | null;
  out_width: number | null;
  out_height: number | null;
  age: number | null;
  note: string;
  candidates: Candidate[];
  auto: Candidate | null;
  selected: string;
  overlay_failures: number;
  python: string | null;
  probe: ProbeInfo;
  overlay_log: string[];
  build_log: string[];
  cache_dir: string;
}

interface ActionResult {
  ok: boolean;
  message: string;
  log?: string;
  path?: string;
}

const getStatus = callable<[], Status>("get_status");
const setSettings = callable<[Partial<Settings>], Settings>("set_settings");
const buildProbe = callable<[], ActionResult>("build_probe");
const removeProbe = callable<[], ActionResult>("remove_probe");
const restartOverlay = callable<[], boolean>("restart_overlay");
const exportLogs = callable<[], ActionResult>("export_logs");

const POSITIONS = [
  { data: "top-right", label: "Top right" },
  { data: "top-left", label: "Top left" },
  { data: "bottom-right", label: "Bottom right" },
  { data: "bottom-left", label: "Bottom left" },
];

const MONO: CSSProperties = {
  fontFamily: "monospace",
  fontSize: "10px",
  lineHeight: 1.4,
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  maxHeight: "140px",
  overflow: "auto",
  opacity: 0.75,
};

function modeText(mode: Status["mode"]): string {
  switch (mode) {
    case "LIVE":
      return "LIVE";
    case "EST":
      return "ESTIMATE";
    case "WAIT":
      return "NO DATA";
    default:
      return "OFF";
  }
}

function modeColor(mode: Status["mode"]): string {
  switch (mode) {
    case "LIVE":
      return "#00e5ff";
    case "EST":
      return "#ffd400";
    case "WAIT":
      return "#ff6b6b";
    default:
      return "#9aa0b4";
  }
}

function Content() {
  const qamVisible = useQuickAccessVisible();
  const [status, setStatus] = useState<Status | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const next = await getStatus();
      setStatus(next);
    } catch (err) {
      console.warn("CyberDRS: get_status failed", err);
    }
  };

  useEffect(() => {
    if (!qamVisible) {
      return;
    }
    let alive = true;
    const tick = async () => {
      try {
        const next = await getStatus();
        if (alive) {
          setStatus(next);
        }
      } catch (err) {
        console.warn("CyberDRS: get_status failed", err);
      }
    };
    tick();
    const handle = setInterval(tick, 1000);
    return () => {
      alive = false;
      clearInterval(handle);
    };
  }, [qamVisible]);

  const update = async (patch: Partial<Settings>) => {
    setStatus((prev) => (prev ? { ...prev, settings: { ...prev.settings, ...patch } } : prev));
    try {
      const merged = await setSettings(patch);
      setStatus((prev) => (prev ? { ...prev, settings: merged } : prev));
    } catch (err) {
      toaster.toast({ title: "CyberDRS", body: `Could not save setting: ${err}`, critical: true });
    }
  };

  const runAction = async (label: string, action: () => Promise<ActionResult>) => {
    setBusy(label);
    try {
      const result = await action();
      toaster.toast({
        title: result.ok ? "CyberDRS" : "CyberDRS error",
        body: result.message,
        critical: !result.ok,
        duration: result.ok ? 5000 : 12000,
      });
      await refresh();
    } catch (err) {
      toaster.toast({ title: "CyberDRS error", body: String(err), critical: true });
    } finally {
      setBusy(null);
    }
  };

  const settings = status?.settings;
  const width = status?.width ?? null;
  const height = status?.height ?? null;
  const outWidth = status?.out_width ?? null;
  const outHeight = status?.out_height ?? null;
  const renderText = width && height ? `${width} × ${height}` : "—";
  const outputText = outWidth && outHeight ? `${outWidth} × ${outHeight}` : "—";
  const scalePct = width && outWidth ? Math.round((width / outWidth) * 100) : null;
  const candidates = status?.candidates ?? [];
  const autoPick = status?.auto ?? candidates[0] ?? null;
  const auto = autoPick ? `${autoPick.w} × ${autoPick.h}` : "—";

  return (
    <>
      <PanelSection title="Status">
        <PanelSectionRow>
          <Field
            label="Source"
            description={status?.note || "Waiting for Cyberpunk 2077"}
          >
            <span style={{ color: modeColor(status?.mode ?? "OFF"), fontWeight: 700 }}>
              {modeText(status?.mode ?? "OFF")}
            </span>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Base render resolution" description={scalePct !== null ? `${scalePct}% of output width` : undefined}>
            <span style={{ fontWeight: 700 }}>{renderText}</span>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Output resolution">
            <span>{outputText}</span>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label="Overlay"
            description={
              status?.overlay
                ? status?.overlay_pid
                  ? `Running (PID ${status.overlay_pid})`
                  : "Running"
                : status?.running
                  ? `Game PID ${status.game_pid} — restarting${
                      (status.overlay_failures ?? 0) > 0
                        ? ` (attempt ${status.overlay_failures + 1}; see Overlay log)`
                        : ""
                    }`
                  : "Overlay stopped"
            }
          >
            <span>{status?.overlay ? "Running" : "Stopped"}</span>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Probe age">
            <span>{status?.age !== null && status?.age !== undefined ? `${status.age.toFixed(1)} s` : "—"}</span>
          </Field>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Render target">
        {candidates.length === 0 ? (
          <PanelSectionRow>
            <Field
              label="No targets seen yet"
              description="Start the game with the probe installed, then wait for a frame or two."
            />
          </PanelSectionRow>
        ) : (
          <>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={() => update({ candidate: "auto" })}
              >
                {settings?.candidate === "auto" ? "● " : "○ "}
                Auto ({auto})
              </ButtonItem>
            </PanelSectionRow>
            {candidates.map((c) => {
              const key = `${c.w}x${c.h}`;
              return (
                <PanelSectionRow key={key}>
                  <ButtonItem
                    layout="below"
                    onClick={() => update({ candidate: key })}
                  >
                    {settings?.candidate === key ? "● " : "○ "}
                    {c.w} × {c.h} · {c.n} target{c.n === 1 ? "" : "s"}
                  </ButtonItem>
                </PanelSectionRow>
              );
            })}
            <PanelSectionRow>
              <Field description="Auto picks the largest target below output size when an upscaled target also exists. Override it here if the game allocates same-aspect intermediate targets." />
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      <PanelSection title="Vulkan probe (LIVE source)">
        <PanelSectionRow>
          <Field
            label="State"
            description={
              status?.probe.installed
                ? "Installed as an implicit layer, Cyberpunk only"
                : "Not installed — resolution is estimated from settings, if available"
            }
          >
            <span style={{ color: status?.probe.installed ? "#00e5ff" : "#ffd400" }}>
              {status?.probe.installed ? (status.probe.live ? "Live" : "Installed") : "Missing"}
            </span>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            label="Build tools"
            description={
              status?.probe.compiler
                ? `${status.probe.compiler}${status.probe.headers ? ", vulkan headers found" : ", vulkan headers missing"}`
                : "No compiler found"
            }
          >
            <span>{status?.probe.compiler && status?.probe.headers ? "Ready" : "Missing"}</span>
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy !== null || !status?.probe.compiler || !status?.probe.headers}
            onClick={() => runAction("build", buildProbe)}
          >
            {busy === "build" ? "Building…" : "Build & install probe"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy !== null || !status?.probe.installed}
            onClick={() => runAction("remove", removeProbe)}
          >
            {busy === "remove" ? "Removing…" : "Remove probe"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            description={
              "Needs gcc and vulkan-headers on the device: sudo dnf install -y gcc vulkan-headers. " +
              "Restart the game after installing or removing. Cyberpunk's own dynamic resolution is a DLSS mode, " +
              "which Adreno cannot use — the probe instead measures the real render target size, so it also " +
              "tracks FSR and dynamic-resolution mods."
            }
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Appearance">
        <PanelSectionRow>
          <ToggleField
            label="Show overlay"
            checked={settings?.enabled ?? true}
            onChange={(value) => update({ enabled: value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Text scale"
            value={settings?.scale ?? 4}
            min={2}
            max={8}
            step={1}
            showValue
            onChange={(value) => update({ scale: value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Background opacity"
            value={settings?.opacity ?? 88}
            min={20}
            max={100}
            step={2}
            showValue
            valueSuffix="%"
            onChange={(value) => update({ opacity: value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Edge margin"
            value={settings?.margin ?? 18}
            min={0}
            max={64}
            step={2}
            showValue
            valueSuffix="px"
            onChange={(value) => update({ margin: value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Position"
            rgOptions={POSITIONS}
            selectedOption={settings?.position ?? "top-right"}
            onChange={(option) => update({ position: String(option.data) })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Show output resolution"
            checked={settings?.show_output ?? true}
            onChange={(value) => update({ show_output: value })}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Diagnostics">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy !== null}
            onClick={() => runAction("export", exportLogs)}
          >
            {busy === "export" ? "Saving…" : "Save logs to Downloads (zip)"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Interpreter" description={status?.python ?? "Chosen when the overlay first starts"} />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Data directory" description={status?.cache_dir ?? "—"} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => runAction("restart", async () => {
            const ok = await restartOverlay();
            return { ok, message: ok ? "Overlay restarted." : "Game is not running." };
          })}>
            {busy === "restart" ? "Restarting…" : "Restart overlay"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Overlay log">
            <div style={MONO}>
              {(status?.overlay_log ?? []).slice(-12).join("\n") || "(empty)"}
            </div>
          </Field>
        </PanelSectionRow>
        {status?.build_log && status.build_log.length > 0 ? (
          <PanelSectionRow>
            <Field label="Build log">
              <div style={MONO}>{status.build_log.slice(-10).join("\n")}</div>
            </Field>
          </PanelSectionRow>
        ) : null}
      </PanelSection>
    </>
  );
}

export default definePlugin(() => ({
  name: "CyberDRS",
  titleView: <div className={staticClasses.Title}>CyberDRS</div>,
  content: <Content />,
  icon: <FaCrosshairs />,
  onDismount() {},
}));
