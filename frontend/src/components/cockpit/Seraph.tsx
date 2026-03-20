// @ts-nocheck
import React, { useState, useEffect } from 'react';
import { motion, AnimatePresence } from 'motion/react';

export type SeraphState = 'idle' | 'thinking' | 'tool_use' | 'approval_wait' | 'error' | 'offline';

export interface SeraphTelemetryEntry {
  label: string;
  value: string;
  hint?: string;
}

interface SeraphProps {
  state: SeraphState;
  detail?: string;
  telemetry?: SeraphTelemetryEntry[];
  statusLabel?: string;
}

const Seraph: React.FC<SeraphProps> = ({
  state,
  detail,
  telemetry,
  statusLabel,
}) => {
  const [frame, setFrame] = useState(0);

  useEffect(() => {
    const interval = setInterval(() => {
      setFrame((f) => (f + 1) % 1000);
    }, 30);
    return () => clearInterval(interval);
  }, []);

  const colorMap = {
    idle: 'text-amber-500',
    thinking: 'text-cyan-400',
    tool_use: 'text-emerald-400',
    approval_wait: 'text-orange-400',
    error: 'text-red-500',
    offline: 'text-zinc-800',
  };

  const barColorMap = {
    idle: '#f59e0b',
    thinking: '#22d3ee',
    tool_use: '#34d399',
    approval_wait: '#fb923c',
    error: '#ef4444',
    offline: '#3f3f46',
  };

  const glowMap = {
    idle: 'rgba(245, 158, 11, 0.3)',
    thinking: 'rgba(34, 211, 238, 0.3)',
    tool_use: 'rgba(16, 185, 129, 0.3)',
    approval_wait: 'rgba(251, 146, 60, 0.3)',
    error: 'rgba(239, 68, 68, 0.3)',
    offline: 'rgba(39, 39, 42, 0.1)',
  };

  const detailMap: Record<SeraphState, string> = {
    idle: 'Guardian linked and awaiting a directive.',
    thinking: 'Inference is active across the current thread.',
    tool_use: 'A tool or workflow step is running now.',
    approval_wait: 'Execution is paused behind approval.',
    error: 'Runtime degraded. Operator attention recommended.',
    offline: 'Workspace transport is offline.',
  };

  const telemetryFallback: SeraphTelemetryEntry[] = [
    { label: 'Context', value: state === 'error' ? 'DEGRADED' : 'GOOD', hint: 'runtime fidelity' },
    { label: 'Queue', value: state === 'approval_wait' ? '01' : 'CLEAR', hint: 'live queue' },
  ];

  const glow = glowMap[state];
  const barColor = barColorMap[state];
  const displayTelemetry = (telemetry?.length ? telemetry : telemetryFallback).slice(0, 2);
  const displayDetail = detail ?? detailMap[state];
  const displayLabel = statusLabel ?? state.toUpperCase().replace('_', ' ');

  const Wing = ({ side, index, state }: { side: 'left' | 'right', index: number, state: SeraphState }) => {
    const isLeft = side === 'left';
    const baseRotation = index * 25 - 25;
    const delay = index * 0.2;
    
    // Paths now start at 0,0 and are designed to look like they emerge from the core
    const paths = [
      { d: isLeft ? "M 0 0 C -40 -40, -120 -60, -160 -10 C -140 20, -60 10, 0 0" : "M 0 0 C 40 -40, 120 -60, 160 -10 C 140 20, 60 10, 0 0", opacity: 0.7, width: 1.5 },
      { d: isLeft ? "M 0 0 C -30 -30, -100 -50, -140 -5 C -120 15, -50 5, 0 0" : "M 0 0 C 30 -30, 100 -50, 140 -5 C 120 15, 50 5, 0 0", opacity: 0.4, width: 1 },
      { d: isLeft ? "M 0 0 C -50 -50, -140 -70, -180 -15 C -160 25, -70 15, 0 0" : "M 0 0 C 50 -50, 140 -70, 180 -15 C 160 25, 70 15, 0 0", opacity: 0.2, width: 2.5 },
    ];

    let animateProps = {};
    let transition: any = { duration: 4 + index, repeat: Infinity, ease: "easeInOut", delay };

    switch (state) {
      case 'idle':
        animateProps = { rotate: [baseRotation, baseRotation + (isLeft ? -8 : 8), baseRotation] };
        break;
      case 'thinking':
        animateProps = { 
          rotate: [baseRotation, baseRotation + (isLeft ? -4 : 4), baseRotation],
          opacity: [0.4, 0.8, 0.4]
        };
        transition = { duration: 0.4, repeat: Infinity, ease: "linear", delay: index * 0.1 };
        break;
      case 'tool_use':
        animateProps = { rotate: baseRotation + (isLeft ? -15 : 15) };
        transition = { duration: 0.5 };
        break;
      case 'approval_wait':
        animateProps = {
          rotate: baseRotation,
          opacity: [0.4, 0.8, 0.4]
        };
        transition = { duration: 2, repeat: Infinity, ease: "easeInOut" };
        break;
      case 'error':
        animateProps = { 
          rotate: [baseRotation, baseRotation + (Math.random() * 40 - 20)],
          opacity: [1, 0, 1, 0.5, 1]
        };
        transition = { duration: 0.05, repeat: Infinity, ease: "linear" };
        break;
      case 'offline':
        animateProps = { rotate: isLeft ? 120 : -120, opacity: 0.05 };
        transition = { duration: 3 };
        break;
    }

    return (
      <motion.g 
        animate={animateProps} 
        transition={transition} 
        style={{ 
          originX: isLeft ? "100%" : "0%", 
          originY: "50%",
          x: isLeft ? -10 : 10 // Slight offset to tuck under the core
        }}
      >
        {paths.map((p, i) => (
          <motion.path
            key={i}
            d={p.d}
            fill="none"
            stroke="currentColor"
            strokeWidth={p.width}
            opacity={p.opacity}
            className="drop-shadow-[0_0_15px_currentColor]"
          />
        ))}
        <motion.path
          d={paths[0].d}
          fill="none"
          stroke="currentColor"
          strokeWidth="0.5"
          strokeDasharray="2 8"
          animate={{ strokeDashoffset: [0, -50] }}
          transition={{ duration: 5, repeat: Infinity, ease: "linear" }}
          opacity={0.3}
        />
      </motion.g>
    );
  };

  const Core = () => {
    return (
      <div className="relative flex items-center justify-center">
        {/* Halo / Aura */}
        <motion.div
          animate={{ 
            scale: state === 'thinking' ? [1, 1.2, 1] : [1, 1.05, 1],
            opacity: state === 'offline' ? 0.05 : [0.1, 0.2, 0.1]
          }}
          transition={{ duration: 3, repeat: Infinity }}
          className={`absolute w-64 h-64 rounded-full bg-current seraph-aura ${colorMap[state]}`}
        />

        {/* Rotating Rings */}
        {[1, 2, 3].map(i => (
          <motion.div
            key={i}
            animate={{ rotate: i % 2 === 0 ? 360 : -360 }}
            transition={{ duration: 10 + i * 5, repeat: Infinity, ease: "linear" }}
            className={`absolute border border-current opacity-20 rounded-full ${colorMap[state]}`}
            style={{ width: 40 + i * 20, height: 40 + i * 20 }}
          />
        ))}

        {/* Central Geometric Core */}
        <motion.div
          animate={state === 'error' ? { x: [0, -4, 4, -2, 2, 0] } : {}}
          transition={{ duration: 0.1, repeat: Infinity }}
          className={`relative z-10 w-16 h-16 glass-panel rounded-full flex items-center justify-center border-2 border-current ${colorMap[state]}`}
        >
          {/* Inner pulsing eye/orb */}
          <motion.div
            animate={{ scale: [1, 1.2, 1], opacity: [0.6, 1, 0.6] }}
            transition={{ duration: 2, repeat: Infinity }}
            className="w-4 h-4 bg-current rounded-full shadow-[0_0_15px_currentColor]"
          />
          
          {/* Internal Details - Simplified */}
          <div className="absolute inset-0 opacity-20">
            <div className="absolute inset-2 border border-current rounded-full" />
            <div className="absolute inset-4 border border-current rounded-full opacity-50" />
          </div>
        </motion.div>

        {/* Thinking Particles */}
        <AnimatePresence>
          {state === 'thinking' && Array.from({ length: 8 }).map((_, i) => (
            <motion.div
              key={i}
              initial={{ scale: 0, opacity: 0, x: 0, y: 0 }}
              animate={{ 
                scale: [0, 1, 0], 
                opacity: [0, 0.5, 0],
                x: Math.cos(i * 45 * Math.PI / 180) * 80,
                y: Math.sin(i * 45 * Math.PI / 180) * 80
              }}
              transition={{ duration: 2, repeat: Infinity, delay: i * 0.2 }}
              className={`absolute w-1 h-1 bg-current rounded-full ${colorMap[state]}`}
            />
          ))}
        </AnimatePresence>
      </div>
    );
  };

  return (
    <div
      className="scanline relative flex h-full w-full min-h-0 flex-col overflow-hidden bg-[#05090d] text-[#dbefff]"
      style={{
        boxShadow: `inset 0 1px 0 rgba(255,255,255,0.02), 0 0 24px ${glow}`,
      }}
    >
      <div className="absolute inset-0 opacity-30 pointer-events-none" style={{
        backgroundImage:
          'linear-gradient(rgba(141,226,255,0.018) 1px, transparent 1px), linear-gradient(90deg, rgba(141,226,255,0.018) 1px, transparent 1px)',
        backgroundSize: '28px 28px',
      }} />
      <div
        className="absolute inset-0 pointer-events-none"
        style={{ background: `radial-gradient(circle at 50% 48%, ${glow}, transparent 46%)` }}
      />

      <div className="relative flex min-h-0 flex-1 items-center justify-center px-3 pb-3 pt-3">
        <div className="relative w-full max-w-[760px] aspect-video flex items-center justify-center float">
          <svg viewBox="-300 -200 600 400" className={`w-full h-full transition-colors duration-700 ${colorMap[state]}`}>
            {[0, 1, 2].map(i => (
              <React.Fragment key={i}>
                <Wing side="left" index={i} state={state} />
                <Wing side="right" index={i} state={state} />
              </React.Fragment>
            ))}
          </svg>

          <div className="absolute">
            <Core />
          </div>
        </div>
      </div>

      <div className="border-t border-[#173547] bg-[linear-gradient(180deg,rgba(7,19,29,0.9),rgba(6,16,25,0.97))] px-4 py-2.5 backdrop-blur-xl">
        <div className="flex flex-wrap items-center justify-between gap-x-5 gap-y-2">
          <div className="min-w-0 flex-1">
            <div className={`text-[18px] font-bold uppercase tracking-[-0.04em] glow-text ${colorMap[state]}`}>
              {displayLabel}
            </div>
            <div className="mt-1 flex gap-1.5">
              {Array.from({ length: 10 }).map((_, i) => (
                <div
                  key={i}
                  className="h-1 w-3 rounded-full transition-all duration-300"
                  style={{
                    background: i < (frame % 10) ? barColor : 'rgba(255,255,255,0.05)',
                    boxShadow: i < (frame % 10) ? `0 0 8px ${barColor}` : 'none',
                  }}
                />
              ))}
            </div>
            <div className="mt-1.5 max-w-[420px] text-[10px] leading-5 text-[#7a98ad]">{displayDetail}</div>
          </div>

          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            {displayTelemetry.map((entry) => (
              <div key={entry.label} className="min-w-[88px] border-l border-[#1f4358] pl-3">
                <div className="text-[8px] uppercase tracking-[0.22em] text-[#55758a]">{entry.label}</div>
                <div className="mt-0.5 text-[13px] font-bold tracking-[-0.03em] text-[#e3f4ff]">{entry.value}</div>
                <div className="text-[8px] uppercase tracking-[0.1em] text-[#6d8ba0]">{entry.hint ?? 'live'}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

export default Seraph;
