"use client";

import { ArrowUp } from "lucide-react";
import { useRef, useState } from "react";

import { Spinner } from "./ui";

interface Props {
  onSubmit: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
  /** The message is on its way: show a spinner instead of the send button. */
  loading?: boolean;
}

export default function ChatInput({ onSubmit, disabled = false, placeholder = "Describe your trip…", loading = false }: Props) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  function submit() {
    const trimmed = value.trim();
    if (!trimmed || disabled || loading) return;
    onSubmit(trimmed);
    setValue("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  }

  function handleInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setValue(e.target.value);
    // grow with the text, up to about six lines
    const el = e.target;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 144)}px`;
  }

  return (
    <div
      className={`flex items-end gap-2 rounded-2xl border border-ink-200 bg-white py-2 pl-4 pr-2 shadow-card transition
                  focus-within:border-ink-900 focus-within:ring-4 focus-within:ring-ink-900/5 ${disabled ? "bg-ink-50" : ""}`}
    >
      <textarea
        ref={textareaRef}
        rows={1}
        value={value}
        onChange={handleInput}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        disabled={disabled || loading}
        aria-label="Message the trip assistant"
        className="max-h-36 flex-1 resize-none self-center bg-transparent py-1.5 text-sm leading-relaxed text-ink-900 outline-none placeholder:text-ink-400 disabled:cursor-not-allowed"
      />

      {loading ? (
        <span className="grid h-9 w-9 shrink-0 place-items-center" role="status" aria-label="Sending">
          <Spinner className="h-4 w-4 text-ink-500" />
        </span>
      ) : (
        <button
          type="button"
          onClick={submit}
          disabled={!value.trim() || disabled}
          aria-label="Send"
          className="focus-ring grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-ink-900 text-white transition
                     hover:bg-ink-800 active:scale-95 disabled:pointer-events-none disabled:bg-ink-200 disabled:text-ink-400"
        >
          <ArrowUp className="h-4 w-4" strokeWidth={2.5} aria-hidden />
        </button>
      )}
    </div>
  );
}
