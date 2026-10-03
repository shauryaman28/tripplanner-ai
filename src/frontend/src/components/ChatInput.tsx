"use client";

import { ArrowUp } from "lucide-react";
import { useId, useRef, useState } from "react";

import { Spinner } from "./ui";

interface Props {
  onSubmit: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
  /** The message is on its way: show a spinner instead of the send button. */
  loading?: boolean;
  /**
   * A lead-in written inside the box, before what the traveller types: "Refine this trip:".
   * It says what a message will do; it is not part of the message that is sent.
   */
  label?: string;
}

export default function ChatInput({ onSubmit, disabled = false, placeholder = "Describe your trip…", loading = false, label }: Props) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const field = useId();

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
      <div className="flex min-w-0 flex-1 flex-col self-center">
        {label && (
          // a <label>, so that a tap on the words lands in the field; the field keeps its own accessible name
          <label htmlFor={field} id={`${field}-label`} className="eyebrow cursor-text pt-0.5">
            {label}
          </label>
        )}
        <textarea
          id={field}
          ref={textareaRef}
          rows={1}
          value={value}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          disabled={disabled || loading}
          aria-label="Message the trip assistant"
          aria-describedby={label ? `${field}-label` : undefined}
          className="max-h-36 w-full resize-none bg-transparent py-1.5 text-sm leading-relaxed text-ink-900 outline-none placeholder:text-ink-400 disabled:cursor-not-allowed"
        />
      </div>

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
