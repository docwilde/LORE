import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

const CARRIER = process.env.LORE_RS || "lore-rs";
const HOOK_TIMEOUT_MS = 15_000;

export default function lore(pi: ExtensionAPI): void {
	let injected = false;
	const reinject = () => {
		injected = false;
	};
	pi.on("session_start", reinject);
	pi.on("session_switch", reinject);
	pi.on("session_clear", reinject);
	pi.on("session_compact", reinject);

	pi.on("before_agent_start", async (event, ctx) => {
		if (event.reason === "revive") return;
		const hookEvent = injected ? "prompt" : "session-start";
		// Set before awaiting: a compaction during the hook must still re-arm injection.
		injected = true;
		try {
			const context = await runHook(hookEvent, { cwd: ctx.cwd, prompt: event.prompt ?? "" });
			if (context) return { message: { customType: "lore", content: context, display: false } };
		} catch (error) {
			pi.logger.warn(`LORE ${hookEvent} hook failed: ${error}`);
		}
	});
}

async function runHook(event: string, input: object): Promise<string | undefined> {
	const child = Bun.spawn([CARRIER, "hook", "--engine", "omp", "--event", event], {
		stdin: new Blob([JSON.stringify(input)]),
		stdout: "pipe",
		stderr: "pipe",
		timeout: HOOK_TIMEOUT_MS,
	});
	const [stdout, stderr, code] = await Promise.all([
		new Response(child.stdout).text(),
		new Response(child.stderr).text(),
		child.exited,
	]);
	if (code !== 0) throw new Error(`${CARRIER} exited ${code}: ${stderr.trim()}`);
	if (!stdout.trim()) return undefined;
	return JSON.parse(stdout).hookSpecificOutput?.additionalContext;
}
