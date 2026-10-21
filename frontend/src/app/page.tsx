import { StatusCard } from "@/components/StatusCard";

export default function Home() {
  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-gradient-to-br from-slate-900 to-slate-800 p-4">
      <main className="w-full max-w-md">
        <h1 className="mb-8 text-center text-3xl font-bold text-white">
          Grounded Answers
        </h1>
        <StatusCard />
      </main>
    </div>
  );
}
