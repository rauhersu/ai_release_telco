"use client";

interface ChatEmptyStateProps {
  onPick: (prompt: string) => void;
  agentLabel?: string;
}

export function ChatEmptyState({ onPick, agentLabel }: ChatEmptyStateProps) {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col items-center justify-center px-4 py-10 text-center md:py-16">
      <p className="text-foreground/45 text-sm">Start a conversation below.</p>
    </div>
  );
}
