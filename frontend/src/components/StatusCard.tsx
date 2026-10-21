"use client";

import { useEffect, useState } from "react";

interface HealthStatus {
  status: "online" | "offline" | "loading";
  message?: string;
}

export function StatusCard() {
  const [health, setHealth] = useState<HealthStatus>({ status: "loading" });

  useEffect(() => {
    const checkHealth = async () => {
      try {
        const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
        const response = await fetch(`${apiUrl}/health`);
        if (response.ok) {
          setHealth({ status: "online" });
        } else {
          setHealth({ status: "offline", message: `HTTP ${response.status}` });
        }
      } catch (error) {
        setHealth({
          status: "offline",
          message: error instanceof Error ? error.message : "Connection failed",
        });
      }
    };

    checkHealth();
  }, []);

  const statusConfig = {
    online: {
      bgColor: "bg-green-50 dark:bg-green-900/20",
      borderColor: "border-green-200 dark:border-green-800",
      textColor: "text-green-900 dark:text-green-100",
      badgeColor: "bg-green-100 dark:bg-green-800",
      badgeText: "Online",
      dotColor: "bg-green-500",
    },
    offline: {
      bgColor: "bg-red-50 dark:bg-red-900/20",
      borderColor: "border-red-200 dark:border-red-800",
      textColor: "text-red-900 dark:text-red-100",
      badgeColor: "bg-red-100 dark:bg-red-800",
      badgeText: "Offline",
      dotColor: "bg-red-500",
    },
    loading: {
      bgColor: "bg-slate-50 dark:bg-slate-900/20",
      borderColor: "border-slate-200 dark:border-slate-800",
      textColor: "text-slate-900 dark:text-slate-100",
      badgeColor: "bg-slate-100 dark:bg-slate-800",
      badgeText: "Checking...",
      dotColor: "bg-slate-400",
    },
  };

  const config = statusConfig[health.status];

  return (
    <div
      className={`rounded-lg border-2 p-6 ${config.bgColor} ${config.borderColor}`}
    >
      <div className="flex items-center justify-between">
        <div>
          <h2 className={`text-lg font-semibold ${config.textColor}`}>
            Backend API
          </h2>
          <p className={`text-sm ${config.textColor} opacity-75`}>
            {health.message || "Connected to backend"}
          </p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <div
            className={`inline-flex items-center gap-2 rounded-full px-3 py-1 ${config.badgeColor}`}
          >
            <div className={`h-2 w-2 rounded-full ${config.dotColor}`} />
            <span className={`text-xs font-medium ${config.textColor}`}>
              {config.badgeText}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
