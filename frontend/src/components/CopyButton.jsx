import { useEffect, useRef, useState } from "react";

// navigator.clipboard is unavailable outside secure contexts, so plain http on a LAN falls back
// to a throwaway textarea and the old execCommand.
async function write(text) {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    const copied = document.execCommand("copy");
    document.body.removeChild(field);
    return copied;
  } catch {
    return false;
  }
}

export default function CopyButton({ value, label = "Copy", title }) {
  const [state, setState] = useState("idle");
  const timer = useRef(null);

  useEffect(() => () => clearTimeout(timer.current), []);

  const onClick = async () => {
    const copied = await write(value);
    setState(copied ? "copied" : "failed");
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), 1800);
  };

  return (
    <button
      type="button"
      className={state === "copied" ? "copy-button copied" : "copy-button"}
      onClick={onClick}
      title={title || `Copy ${value}`}
    >
      <span aria-live="polite">
        {state === "copied" ? "Copied!" : state === "failed" ? "Copy failed" : label}
      </span>
    </button>
  );
}
