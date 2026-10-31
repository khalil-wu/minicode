using System;
using System.Threading;

// A real owned transport fixture. It deliberately does not pretend to spawn a PTY.
public static class WindowsPtyOwnerFixture {
    public static void Main() {
        Console.ReadLine();
        Console.WriteLine("{\"event\":\"ready\",\"identity\":{\"identity\":\"minicode-desktop-pty\",\"protocol\":1,\"sourceVersion\":\"0.158.0-alpha.2.1\"}}");
        Console.Out.Flush();
        var mode = Environment.GetEnvironmentVariable("MINICODE_TEST_PTY_OWNER_MODE");
        if (mode == "transport-error") Environment.Exit(23);
        if (mode == "missing-result") return;
        if (mode == "invalid-protocol") {
            Console.WriteLine("invalid-json");
            Console.Out.Flush();
        }
        while (Console.ReadLine() != null) { }
        if (mode == "ignore-terminate") Thread.Sleep(120000);
    }
}
