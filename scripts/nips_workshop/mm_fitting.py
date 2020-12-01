# =============================================================================
# IMPORTS
# =============================================================================
import argparse
import os

import numpy as np
import torch

import espaloma as esp


def run(args):
    # define data
    data = getattr(esp.data, args.data)(first=args.first)

    # get force field
    forcefield = esp.graphs.legacy_force_field.LegacyForceField(
        args.forcefield
    )

    # param / typing
    operation = forcefield.parametrize

    # apply to dataset
    data = data.apply(operation, in_place=True)

    # apply simulation
    # make simulation
    from espaloma.data.md import MoleculeVacuumSimulation
    simulation = MoleculeVacuumSimulation(
        n_samples=100, n_steps_per_sample=10,
    )

    data = data.apply(simulation.run, in_place=True)

    # split
    partition = [int(x) for x in args.partition.split(":")]
    ds_tr, ds_te = data.split(partition)

    # batch
    ds_tr = ds_tr.view("graph", batch_size=args.batch_size)
    ds_te = ds_te.view("graph", batch_size=args.batch_size)

    # layer
    layer = esp.nn.layers.dgl_legacy.gn(args.layer)

    # representation
    representation = esp.nn.Sequential(layer, config=args.config)

    # get the last bit of units
    units = [int(x) for x in args.config if x.isdigit()][-1]

    print(args.janossy_config)

    janossy_config = []
    for x in args.janossy_config:
        if isinstance(x, int):
            janossy_config.append(int(x))

        elif x.isdigit():
            janossy_config.append(int(x))

        else:
            janossy_config.append(x)

    print(janossy_config)

    readout = esp.nn.readout.janossy.JanossyPooling(
        in_features=units, config=janossy_config,
    )

    net = torch.nn.Sequential(
            representation, 
            readout,
            esp.mm.geometry.GeometryInGraph(),
            esp.mm.energy.EnergyInGraph(terms=["n2", "n3"]),
            esp.mm.energy.EnergyInGraph(terms=["n2", "n3"], suffix='_ref'),
    )
   
    metrics_tr = [
        esp.metrics.GraphMetric(
            base_metric=torch.nn.MSELoss(),
            between=['u', "u_ref"],
            level="g",
        ),

        
        esp.metrics.GraphDerivativeMetric(
              base_metric=torch.nn.MSELoss(),
              between=["u", "u_ref"],
              level="g",
              weight=10.0,
        ),
    ]


    metrics_te = [
        esp.metrics.GraphMetric(
            base_metric=esp.metrics.r2,
            between=['u', 'u_ref'],
            level="g",
        ),
        esp.metrics.GraphMetric(
            base_metric=esp.metrics.rmse,
            between=['u', 'u_ref'],
            level="g",
        ),

    ]

    exp = esp.TrainAndTest(
        ds_tr=ds_tr,
        ds_te=ds_te,
        net=net,
        metrics_tr=metrics_tr,
        metrics_te=metrics_te,
        n_epochs=args.n_epochs,
        normalize=esp.data.normalize.NotNormalize,
        optimizer=lambda net: torch.optim.Adam(net.parameters(), 1e-3),
        device=torch.device('cuda:0'),
    )

    results = exp.run()

    print(esp.app.report.markdown(results))

    import os
    os.mkdir(args.out)

    with open(args.out + "/architecture.txt", "w") as f_handle:
        f_handle.write(str(exp))

    with open(args.out + "/result_table.md", "w") as f_handle:
        f_handle.write(esp.app.report.markdown(results))

    curves = esp.app.report.curve(results)

    for spec, curve in curves.items():
        np.save(args.out + "/" + "_".join(spec) + ".npy", curve)

    import pickle
    with open(args.out + "/ref_g_test.th", "wb") as f_handle:
        pickle.dump(exp.ref_g_test, f_handle)

    with open(args.out + "/ref_g_training.th", "wb") as f_handle:
        pickle.dump(exp.ref_g_training, f_handle)


    print(esp.app.report.markdown(results))

    import pickle
    with open(args.out + "/ref_g_test.th", "wb") as f_handle:
        pickle.dump(exp.ref_g_test, f_handle)

    with open(args.out + "/ref_g_training.th", "wb") as f_handle:
        pickle.dump(exp.ref_g_training, f_handle)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="alkethoh", type=str)
    parser.add_argument("--first", default=-1, type=int)
    parser.add_argument("--partition", default="4:1", type=str)
    parser.add_argument("--batch_size", default=8, type=int)
    parser.add_argument("--forcefield", default="smirnoff99Frosst", type=str)
    parser.add_argument("--layer", default="GraphConv", type=str)
    parser.add_argument("--n_classes", default=100, type=int)
    parser.add_argument(
        "--config", nargs="*", default=[32, "tanh", 32, "tanh", 32, "tanh"]
    )

    parser.add_argument(
        "--training_metrics", nargs="*", default=["TypingCrossEntropy"]
    )
    parser.add_argument(
        "--test_metrics", nargs="*", default=["TypingAccuracy"]
    )
    parser.add_argument(
        "--out", default="results", type=str
    )
    parser.add_argument("--janossy_config", nargs="*", default=[32, "leaky_relu"])

    parser.add_argument("--n_epochs", default=10, type=int)

    args = parser.parse_args()

    run(args)
