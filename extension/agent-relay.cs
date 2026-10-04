using System;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;

// Loopback-only, fixed-destination TCP relay. No credentials or payloads are logged.
// A process outside the VPN carries only the feeder's local agent connection.
public static class MutAgentRelay {
    public static void Run(string host, int upstreamPort, int listenPort) {
        var listener = new TcpListener(IPAddress.Loopback, listenPort);
        var slots = new SemaphoreSlim(32, 32);
        listener.Start(32);
        try {
            while (true) {
                var client = listener.AcceptTcpClient();
                if (!slots.Wait(0)) { client.Close(); continue; }
                Task.Run(() => Forward(client, host, upstreamPort, slots));
            }
        } finally { listener.Stop(); }
    }

    private static async Task Forward(TcpClient client, string host, int port, SemaphoreSlim slots) {
        using (client)
        using (var upstream = new TcpClient()) {
            try {
                var connect = upstream.ConnectAsync(host, port);
                if (await Task.WhenAny(connect, Task.Delay(5000)) != connect) return;
                await connect;
                var incoming = client.GetStream();
                var outgoing = upstream.GetStream();
                var send = incoming.CopyToAsync(outgoing);
                var receive = outgoing.CopyToAsync(incoming);
                // Bound idle/abandoned connections; disposal interrupts both copies.
                await Task.WhenAny(send, receive, Task.Delay(60000));
            } catch (Exception) {
                // The browser receives a failed connection and retries through its
                // normal scheduler. Never log headers, tokens, or response bodies.
            } finally { slots.Release(); }
        }
    }
}
