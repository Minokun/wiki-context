import React from "react";

export const WctxMark: React.FC<{ className?: string }> = ({ className = "h-6 w-6" }) => (
  <span
    className={`inline-flex shrink-0 items-center justify-center rounded-[7px] bg-[#1A56DB] text-white shadow-xs ${className}`}
    aria-hidden="true"
  >
    <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 6l4 12 4-9 4 9 4-12" />
    </svg>
  </span>
);

export const BrandGlyph: React.FC<{
  brand: string;
  surface?: "light" | "dark";
  className?: string;
}> = ({ brand, surface, className = "h-4 w-4" }) => {
  const key = (brand || "").toLowerCase();
  const monoColor = surface ? (surface === "dark" ? "#E2E8F0" : "#181C25") : "var(--brand-mono)";

  if (key.includes("claude")) {
    return (
      <svg fill="#D97757" viewBox="0 0 24 24" className={`shrink-0 ${className}`} aria-hidden="true">
        <path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z" />
      </svg>
    );
  }

  if (key.includes("cursor")) {
    return (
      <svg fill={monoColor} viewBox="0 0 24 24" className={`shrink-0 ${className}`} aria-hidden="true">
        <path d="M11.503.131 1.891 5.678a.84.84 0 0 0-.42.726v11.188c0 .3.162.575.42.724l9.609 5.55a1 1 0 0 0 .998 0l9.61-5.55a.84.84 0 0 0 .42-.724V6.404a.84.84 0 0 0-.42-.726L12.497.131a1.01 1.01 0 0 0-.996 0M2.657 6.338h18.55c.263 0 .43.287.297.515L12.23 22.918c-.062.107-.229.064-.229-.06V12.335a.59.59 0 0 0-.295-.51l-9.11-5.257c-.109-.063-.064-.23.061-.23" />
      </svg>
    );
  }

  if (key.includes("codex") || key.includes("openai")) {
    return (
      <svg fill={monoColor} viewBox="0 0 24 24" className={`shrink-0 ${className}`} aria-hidden="true">
        <path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z" />
      </svg>
    );
  }

  if (key.includes("gemini")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="#4285F4" aria-hidden="true">
        <path d="M12 2c1.2 4.8 4.2 7.8 9 9-4.8 1.2-7.8 4.2-9 9-1.2-4.8-4.2-7.8-9-9 4.8-1.2 7.8-4.2 9-9Z" />
      </svg>
    );
  }

  if (key.includes("agy") || key.includes("antigravity") || key.includes("google")) {
    return (
      <svg fill="#4285F4" viewBox="0 0 24 24" className={`shrink-0 ${className}`} aria-hidden="true">
        <path d="M12.48 10.92v3.28h7.84c-.24 1.84-.853 3.187-1.787 4.133-1.147 1.147-2.933 2.4-6.053 2.4-4.827 0-8.6-3.893-8.6-8.72s3.773-8.72 8.6-8.72c2.6 0 4.507 1.027 5.907 2.347l2.307-2.307C18.747 1.44 16.133 0 12.48 0 5.867 0 .307 5.387.307 12s5.56 12 12.173 12c3.573 0 6.267-1.173 8.373-3.36 2.16-2.16 2.84-5.213 2.84-7.667 0-.76-.053-1.467-.173-2.053H12.48z" />
      </svg>
    );
  }

  if (key.includes("vscode")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="#007ACC" aria-hidden="true">
        <path d="M17.5 2L7.7 11.1 3.7 8.1 2 9.4l4.1 3.1L2 15.6l1.7 1.3 4-3 9.8 9.1L22 20.8V4.2L17.5 2Zm0 4.7v11.6l-6.6-5.8 6.6-5.8Z" />
      </svg>
    );
  }

  if (key.includes("windsurf")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
        <rect x="2" y="2" width="20" height="20" rx="5" fill="#09B6A2" />
        <path d="M6 9.5c2 0 2.5 5 4.5 5s2.5-5 4.5-5 2 3.5 3 3.5" stroke="#FFFFFF" strokeWidth="2.1" strokeLinecap="round" />
      </svg>
    );
  }

  if (key.includes("trae")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
        <rect x="2" y="2" width="20" height="20" rx="5" fill="#10B981" />
        <path d="M7.5 8.5h9M12 8.5v7.5" stroke="#FFFFFF" strokeWidth="2.3" strokeLinecap="round" />
      </svg>
    );
  }

  if (key.includes("kiro")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
        <rect x="2" y="2" width="20" height="20" rx="5" fill="#7C3AED" />
        <path d="M8.5 7v10M15.5 7.5L10 12l5.5 4.5" stroke="#FFFFFF" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  if (key.includes("cline")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
        <rect x="2" y="2" width="20" height="20" rx="5" fill="#0284C7" />
        <path d="M15.5 8.5a4.8 4.8 0 1 0 0 7" stroke="#FFFFFF" strokeWidth="2.2" strokeLinecap="round" />
      </svg>
    );
  }

  if (key.includes("roo")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
        <rect x="2" y="2" width="20" height="20" rx="5" fill="#EA580C" />
        <path d="M8.5 16.5V7.5h4a2.8 2.8 0 0 1 0 5.6H8.5M12.8 13.1l2.7 3.4" stroke="#FFFFFF" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  if (key.includes("qwen")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
        <polygon points="12,2.5 20.5,7.5 20.5,16.5 12,21.5 3.5,16.5 3.5,7.5" fill="#6366F1" />
        <circle cx="12" cy="12" r="3.6" stroke="#FFFFFF" strokeWidth="2" />
      </svg>
    );
  }

  if (key.includes("kimi")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
        <circle cx="12" cy="12" r="10" fill={surface === "dark" ? "#334155" : "#111827"} />
        <path d="M8.5 7.5v9M15 8l-4.5 4 4.8 4.5" stroke="#FFFFFF" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  if (key.includes("opencode")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
        <rect x="2.5" y="2.5" width="19" height="19" rx="5" stroke={monoColor} strokeWidth="2" />
        <path d="M9 9l-3 3 3 3M15 9l3 3-3 3" stroke={monoColor} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  if (key.includes("zed")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
        <rect x="2" y="2" width="20" height="20" rx="5" fill="#084CCF" />
        <path d="M7.5 8h9l-9 8h9" stroke="#FFFFFF" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  if (key.includes("lmstudio")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
        <rect x="2" y="2" width="20" height="20" rx="5" fill="#4F46E5" />
        <path d="M7.5 7.5v9h4.5M14 16.5V8.5l3 4 2.5-4v8" stroke="#FFFFFF" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  if (key.includes("obsidian")) {
    return (
      <svg fill="#7C3AED" viewBox="0 0 24 24" className={`shrink-0 ${className}`} aria-hidden="true">
        <path d="M19.355 18.538a68.967 68.959 0 0 0 1.858-2.954.81.81 0 0 0-.062-.9c-.516-.685-1.504-2.075-2.042-3.362-.553-1.321-.636-3.375-.64-4.377a1.707 1.707 0 0 0-.358-1.05l-3.198-4.064a3.744 3.744 0 0 1-.076.543c-.106.503-.307 1.004-.536 1.5-.134.29-.29.6-.446.914l-.31.626c-.516 1.068-.997 2.227-1.132 3.59-.124 1.26.046 2.73.815 4.481.128.011.257.025.386.044a6.363 6.363 0 0 1 3.326 1.505c.916.79 1.744 1.922 2.415 3.5zM8.199 22.569c.073.012.146.02.22.02.78.024 2.095.092 3.16.29.87.16 2.593.64 4.01 1.055 1.083.316 2.198-.548 2.355-1.664.114-.814.33-1.735.725-2.58l-.01.005c-.67-1.87-1.522-3.078-2.416-3.849a5.295 5.295 0 0 0-2.778-1.257c-1.54-.216-2.952.19-3.84.45.532 2.218.368 4.829-1.425 7.531zM5.533 9.938c-.023.1-.056.197-.098.29L2.82 16.059a1.602 1.602 0 0 0 .313 1.772l4.116 4.24c2.103-3.101 1.796-6.02.836-8.3-.728-1.73-1.832-3.081-2.55-3.831zM9.32 14.01c.615-.183 1.606-.465 2.745-.534-.683-1.725-.848-3.233-.716-4.577.154-1.552.7-2.847 1.235-3.95.113-.235.223-.454.328-.664.149-.297.288-.577.419-.86.217-.47.379-.885.46-1.27.08-.38.08-.72-.014-1.043-.095-.325-.297-.675-.68-1.06a1.6 1.6 0 0 0-1.475.36l-4.95 4.452a1.602 1.602 0 0 0-.513.952l-.427 2.83c.672.59 2.328 2.316 3.335 4.711.09.21.175.43.253.653z" />
      </svg>
    );
  }

  if (key.includes("tailscale")) {
    return (
      <svg fill={monoColor} viewBox="0 0 24 24" className={`shrink-0 ${className}`} aria-hidden="true">
        <path d="M24 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm-9 9a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm0-9a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm6-6a3 3 0 1 1 0-6 3 3 0 0 1 0 6zm0-.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM3 24a3 3 0 1 1 0-6 3 3 0 0 1 0 6zm0-.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zm18 .5a3 3 0 1 1 0-6 3 3 0 0 1 0 6zm0-.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM6 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm9-9a3 3 0 1 1-6 0 3 3 0 0 1 6 0zm-3 2.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM6 3a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM3 5.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z" />
      </svg>
    );
  }

  if (key.includes("dsh") || key.includes("deepseek")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
        <rect x="2" y="2" width="20" height="20" rx="5" fill="#1A56DB" />
        <path d="M7 8.5L11 12L7 15.5M12.5 15.5H17" stroke="#FFFFFF" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  if (key.includes("workbuddy")) {
    return (
      <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
        <rect x="2" y="2" width="20" height="20" rx="5" fill={surface === "dark" ? "#0E7490" : "#0891B2"} />
        <path d="M6.5 8.5L9 15.5L12 10L15 15.5L17.5 8.5" stroke="#FFFFFF" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  return (
    <svg viewBox="0 0 24 24" className={`shrink-0 ${className}`} fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke={surface === "dark" ? "#22D3EE" : "#1A56DB"} strokeWidth="2.1" />
      <circle cx="12" cy="12" r="3" fill={surface === "dark" ? "#F59E0B" : "#1A56DB"} />
    </svg>
  );
};
