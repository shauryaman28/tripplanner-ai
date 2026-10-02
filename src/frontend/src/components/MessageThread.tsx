"use client";

import { Sparkles } from "lucide-react";

export interface Message {
  role: "user" | "assistant" | "system";
  text: string;
  id: string;
}

interface Props {
  messages: Message[];
}

export function AssistantAvatar({ size = "h-7 w-7" }: { size?: string }) {
  return (
    <span className={`grid shrink-0 place-items-center rounded-full bg-sunset text-white shadow-sm ${size}`} aria-hidden>
      <Sparkles className="h-[55%] w-[55%]" strokeWidth={2.2} />
    </span>
  );
}

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-2xl rounded-br-md bg-ink-900 px-3.5 py-2.5">
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-white">{text}</p>
      </div>
    </div>
  );
}

function AssistantBubble({ text }: { text: string }) {
  return (
    <div className="flex items-start gap-2.5">
      <AssistantAvatar />
      <div className="max-w-[85%] rounded-2xl rounded-tl-md bg-ink-100 px-3.5 py-2.5">
        <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink-800">{text}</p>
      </div>
    </div>
  );
}

function SystemNote({ text }: { text: string }) {
  return (
    <div className="flex justify-center">
      <span className="rounded-full bg-ink-100 px-3 py-1 text-xs text-ink-500">{text}</span>
    </div>
  );
}

/** The conversation. Scrolling it is the caller's business: it owns the scroll area. */
export default function MessageThread({ messages }: Props) {
  if (messages.length === 0) return null;

  return (
    <div className="space-y-3" aria-live="polite">
      {messages.map((msg) => (
        <div key={msg.id} className="animate-rise">
          {msg.role === "user" ? (
            <UserBubble text={msg.text} />
          ) : msg.role === "assistant" ? (
            <AssistantBubble text={msg.text} />
          ) : (
            <SystemNote text={msg.text} />
          )}
        </div>
      ))}
    </div>
  );
}
