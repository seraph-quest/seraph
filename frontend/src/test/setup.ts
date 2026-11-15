import "@testing-library/jest-dom";
import { configure } from "@testing-library/react";

// Loaded CI runners routinely take longer than Testing Library's 1s default to
// render cockpit panels after their mocked loads resolve. A longer async
// budget only delays failure reporting; it never relaxes an assertion. The
// per-test vitest testTimeout (20s) still bounds every test.
configure({ asyncUtilTimeout: 5000 });
