import { configure } from "@testing-library/react";

// ASGI bridges start a real Python process; allow bounded cold-start time.
configure({ asyncUtilTimeout: 5000 });
