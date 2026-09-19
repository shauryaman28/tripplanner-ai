"use client";

import { useRef, useState } from "react";

interface Props {
  onSubmit: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
  /** When true, shows a pulsing "thinking" indicator instead of the send button */
  loading?: boolean;
}

export default function ChatInput({
  onSubmit,
  disabled = false,
  placeholder = "Describe your trip…",
  loading = false,
}: Props) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  function submit() {
    const trimmed = value.trim();
    if (!trimmed || disabled || loading) return;
    onSubmit(trimmed);
    setValue("");
    // Reset textarea height
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  }

  function handleInput(e: React.ChangeEvent<HTMLTextAreaElement>) {
    setValue(e.target.value);
    // Auto-grow up to 6 rows
    const el = e.target;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 144)}px`;
  }

  return (
    <div
      className={`flex items-end gap-3 rounded-2xl border bg-white px-4 py-3 shadow-sm
                  transition-shadow focus-within:shadow-md focus-within:border-brand-300
                  ${disabled ? "opacity-60" : ""}`}
    >
      <textarea
        ref={textareaRef}
        rows={1}
        value={value}
        onChange={handleInput}
        onKeyDown={handleKeyDown}
        placeholder={placeholder}
        disabled={disabled || loading}
        className="flex-1 resize-none bg-transparent text-sm text-gray-900
                   placeholder:text-gray-400 outline-none leading-relaxed"
        aria-label="Trip description"
      />

      {loading ? (
        <div className="mb-0.5 flex items-center gap-1 pr-1" aria-label="Thinking">
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              className="h-1.5 w-1.5 rounded-full bg-brand-400 animate-pulse-dot"
              style={{ animationDelay: `${i * 0.2}s` }}
            />
          ))}
        </div>
      ) : (
        <button
          type="button"
          onClick={submit}
          disabled={!value.trim() || disabled}
          aria-label="Send"
          className="mb-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-xl
                     bg-brand-600 text-white shadow-sm
                     hover:bg-brand-700 active:scale-95
                     disabled:opacity-30 disabled:pointer-events-none
                     transition-all duration-150 focus-ring"
        >
          <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
            <path d="M3.105 2.289a.75.75 0 00-.826.95l1.903 6.557H13.5a.75.75 0 010 1.5H4.182l-1.903 6.557a.75.75 0 00.826.95 28.896 28.896 0 0015.293-7.154.75.75 0 000-1.115A28.897 28.897 0 003.105 2.289z" />
          </svg>
        </button>
      )}
    </div>
  );
}
