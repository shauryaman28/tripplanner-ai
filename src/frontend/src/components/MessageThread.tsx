"use client";

export interface Message {
  role: "user" | "assistant" | "system";
  text: string;
  id: string;
}

interface Props {
  messages: Message[];
}

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[80%] rounded-2xl rounded-tr-sm bg-brand-600 px-4 py-2.5">
        <p className="text-sm text-white leading-relaxed whitespace-pre-wrap">{text}</p>
      </div>
    </div>
  );
}

function AssistantBubble({ text }: { text: string }) {
  return (
    <div className="flex items-start gap-2.5">
      {/* Avatar */}
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-brand-500 to-accent-500">
        <svg className="h-3.5 w-3.5 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
        </svg>
      </div>
      <div className="max-w-[80%] rounded-2xl rounded-tl-sm bg-white border border-gray-100 px-4 py-2.5 shadow-sm">
        <p className="text-sm text-gray-800 leading-relaxed whitespace-pre-wrap">{text}</p>
      </div>
    </div>
  );
}

function SystemBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-center">
      <span className="rounded-full bg-gray-100 px-3 py-1 text-xs text-muted">{text}</span>
    </div>
  );
}

export default function MessageThread({ messages }: Props) {
  if (messages.length === 0) return null;

  return (
    <div className="space-y-3">
      {messages.map((msg) => {
        if (msg.role === "user")      return <UserBubble      key={msg.id} text={msg.text} />;
        if (msg.role === "assistant") return <AssistantBubble key={msg.id} text={msg.text} />;
        return                               <SystemBubble    key={msg.id} text={msg.text} />;
      })}
    </div>
  );
}
